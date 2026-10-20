"""Базовый класс тестов, запрещающий любые сетевые обращения.

Сервис работает с продуктивной базой 1С, поэтому ни один тест не должен уметь
выйти наружу — ни через `urllib`, которым пользуется `JsonHttpClient`, ни мимо
него. Поэтому перехватываются и сам `urlopen`, и `socket.connect` под ним.
"""

import socket
import unittest
from unittest import mock

_BLOCKED_TARGETS = (
    "app.clients.http_json.urlopen",
    "urllib.request.urlopen",
    "smtplib.SMTP",
    "smtplib.SMTP_SSL",
)


def _blocked(*args, **kwargs):
    raise AssertionError("Сетевой вызов запрещён в тестах")


class _GuardMixin:
    def setUp(self):
        super().setUp()
        for target in _BLOCKED_TARGETS:
            patcher = mock.patch(target, side_effect=_blocked)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Последний рубеж: ловит всё, что обошло перехваченные выше обёртки.
        patcher = mock.patch.object(socket.socket, "connect", side_effect=_blocked)
        patcher.start()
        self.addCleanup(patcher.stop)


class NetworkGuardTestCase(_GuardMixin, unittest.TestCase):
    pass


class AsyncNetworkGuardTestCase(_GuardMixin, unittest.IsolatedAsyncioTestCase):
    """То же для тестов, которым нужен живой event loop (планировщик)."""
