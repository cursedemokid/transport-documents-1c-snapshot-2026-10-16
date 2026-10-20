"""Инициализация логирования.

Без неё все `logger.info` теряются, а предупреждения уходят через
`logging.lastResort` без времени и уровня — ночной прогон нечем диагностировать.
"""

import logging
import os
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app import bootstrap
from tests.support.no_network import NetworkGuardTestCase


class LoggingSetupTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        saved_flag = bootstrap._CONFIGURED

        def restore():
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
            bootstrap._CONFIGURED = saved_flag

        self.addCleanup(restore)
        bootstrap._CONFIGURED = False

    def test_root_logger_gets_a_handler(self):
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "INFO"}, clear=True):
            bootstrap.configure_logging(force=True)

        self.assertTrue(logging.getLogger().handlers)

    def test_repeated_call_does_not_duplicate_handlers(self):
        """uvicorn --reload и тесты зовут инициализацию не один раз."""
        with mock.patch.dict(os.environ, {}, clear=True):
            bootstrap.configure_logging(force=True)
            before = len(logging.getLogger().handlers)
            bootstrap.configure_logging()
            bootstrap.configure_logging()

        self.assertEqual(before, len(logging.getLogger().handlers))

    def test_records_reach_the_handler_with_level_and_name(self):
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "INFO"}, clear=True):
            bootstrap.configure_logging(force=True)

        with self.assertLogs("app.проверка", level="INFO") as logs:
            logging.getLogger("app.проверка").info("сообщение")

        self.assertIn("сообщение", "\n".join(logs.output))

    def test_log_file_is_created(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "вложенный" / "service.log"
            with mock.patch.dict(os.environ, {"LOG_FILE": str(path)}, clear=True):
                bootstrap.configure_logging(force=True)
                logging.getLogger("app.файл").warning("в файл")

            self.assertTrue(path.exists())
            self.assertIn("в файл", path.read_text(encoding="utf-8"))

    def test_broken_numeric_env_does_not_break_startup(self):
        """Логирование не должно падать раньше, чем успеет сообщить о проблеме.

        Проверять надо переменную, которую код действительно читает. Раньше тут
        стояла `LOG_MAX_BYTES` — такой настройки нет (ротация суточная, а не по
        размеру), и тест проходил вхолостую, ничего не проверяя.
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "s.log"
            env = {"LOG_FILE": str(path), "LOG_BACKUP_DAYS": "много"}
            with mock.patch.dict(os.environ, env, clear=True):
                bootstrap.configure_logging(force=True)
                logging.getLogger("app.проверка").warning("строка после битой настройки")

            self.assertTrue(logging.getLogger().handlers)
            self.assertIn("строка после битой настройки", path.read_text(encoding="utf-8"))


class SocketTimeoutTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        saved = socket.getdefaulttimeout()
        self.addCleanup(lambda: socket.setdefaulttimeout(saved))

    def test_timeout_is_applied(self):
        """pysaby зовёт urlopen без таймаута — нужен общий предел."""
        bootstrap.configure_socket_timeout(12.5)

        self.assertEqual(12.5, socket.getdefaulttimeout())

    def test_zero_is_ignored(self):
        socket.setdefaulttimeout(None)
        bootstrap.configure_socket_timeout(0)

        self.assertIsNone(socket.getdefaulttimeout())


if __name__ == "__main__":
    unittest.main()
