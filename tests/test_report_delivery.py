"""Доставка отчёта: почему письмо ушло или не ушло.

Раньше `send()` возвращал голый bool, «не настроено» было неотличимо от
«отправлено», и ни один отказ не попадал в лог.
"""

import unittest
from datetime import date
from pathlib import Path
import os
import stat
from tempfile import TemporaryDirectory
from unittest import mock

from app.schemas.sync import SyncItemResult, SyncRunResponse
from app.services.reporting import ReportService
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import test_settings


def _report(**overrides) -> SyncRunResponse:
    params = {
        "request_id": "r-1",
        "dry_run": True,
        "writes_enabled": False,
        "date_from": date(2026, 9, 1),
        "date_to": date(2026, 9, 15),
        "total_saby_documents": 2,
        "results": [
            SyncItemResult(status="dry_run_ready", message="ок"),
            SyncItemResult(status="manual", message="склад"),
        ],
    }
    params.update(overrides)
    return SyncRunResponse(**params)


class DeliveryOutcomeTests(NetworkGuardTestCase):
    def _service(self, directory: str, **overrides) -> ReportService:
        return ReportService(test_settings(report_dir=directory, **overrides))

    def test_backend_none_is_not_reported_as_sent(self):
        """Регрессия: возвращался True, и в ответе API стояло report_sent=true."""
        with TemporaryDirectory() as directory:
            delivery = self._service(directory, report_backend="none").send(_report())

        self.assertFalse(delivery.sent)
        self.assertIn("REPORT_BACKEND=none", delivery.reason)
        self.assertIsNotNone(delivery.path)

    def test_file_is_saved_even_when_nothing_is_delivered(self):
        with TemporaryDirectory() as directory:
            delivery = self._service(directory, report_backend="none").send(_report())

            self.assertEqual(1, len(list(Path(directory).glob("*"))))
            self.assertTrue(Path(delivery.path).exists())

    def test_missing_smtp_host_is_logged_not_swallowed(self):
        """Регрессия: отказ был тихим — ни в ответе, ни в логе следа."""
        with TemporaryDirectory() as directory:
            service = self._service(
                directory, report_backend="smtp", smtp_host=None, smtp_to=["a@b.c"]
            )
            with self.assertLogs("app.services.reporting", level="ERROR") as logs:
                delivery = service.send(_report())

        self.assertFalse(delivery.sent)
        self.assertIn("SMTP_HOST", delivery.reason)
        self.assertIn("SMTP_HOST", "\n".join(logs.output))

    def test_missing_recipients_are_reported(self):
        with TemporaryDirectory() as directory:
            service = self._service(
                directory, report_backend="smtp", smtp_host="mail.local", smtp_to=[]
            )
            with self.assertLogs("app.services.reporting", level="ERROR"):
                delivery = service.send(_report())

        self.assertFalse(delivery.sent)
        self.assertIn("получатели", delivery.reason)

    def test_console_backend_delivers(self):
        with TemporaryDirectory() as directory:
            delivery = self._service(directory, report_backend="console").send(_report())

        self.assertTrue(delivery.sent)
        self.assertIn("консоль", delivery.reason)

    def test_both_is_not_sent_when_smtp_fails(self):
        """Печать в консоль не заменяет письма.

        Раньше `both` ставил признак доставки от одной печати в stdout, и
        упавший SMTP отчитывался как «отправлено». Проверяют настройки почты
        обычно именно на `both` — то есть проверка врала ровно там, где её
        делают. Просили письмо — доставку решает почта.
        """
        with TemporaryDirectory() as directory:
            service = self._service(
                directory, report_backend="both", smtp_host=None, smtp_to=["a@b.c"]
            )
            with self.assertLogs("app.services.reporting", level="ERROR"):
                delivery = service.send(_report())

        self.assertFalse(delivery.sent)
        # Обе половины исхода видны: печать прошла, почта — нет и почему.
        self.assertIn("консоль", delivery.reason)
        self.assertIn("SMTP_HOST", delivery.reason)


class SmtpTests(NetworkGuardTestCase):
    def _send(self, directory, **overrides):
        settings = test_settings(
            report_dir=directory,
            report_backend="smtp",
            smtp_host="mail.local",
            smtp_to=["developer@example.com"],
            smtp_from="developer@example.com",
            **overrides,
        )
        with mock.patch("smtplib.SMTP") as smtp_class:
            delivery = ReportService(settings).send(_report())
        return delivery, smtp_class

    def test_timeout_is_passed(self):
        """Без таймаута зависший SMTP держит поток планировщика намертво."""
        with TemporaryDirectory() as directory:
            delivery, smtp_class = self._send(directory, smtp_timeout_seconds=17.0)

        self.assertTrue(delivery.sent)
        self.assertEqual(17.0, smtp_class.call_args.kwargs["timeout"])

    def test_recipients_include_cc_and_bcc(self):
        with TemporaryDirectory() as directory:
            _, smtp_class = self._send(
                directory, smtp_cc=["developer@example.com"], smtp_bcc=["developer@example.com"]
            )

        sent = smtp_class.return_value.__enter__.return_value.send_message
        self.assertEqual(
            ["developer@example.com", "developer@example.com", "developer@example.com"],
            sent.call_args.kwargs["to_addrs"],
        )

    def test_bcc_is_not_exposed_in_headers(self):
        with TemporaryDirectory() as directory:
            _, smtp_class = self._send(directory, smtp_bcc=["скрытый@example.com"])

        message = smtp_class.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertIsNone(message["Bcc"])

    def test_network_failure_becomes_a_reason(self):
        """Сбой сети — такой же «не доставлено», а не вылетевшее наружу исключение."""
        import socket

        with TemporaryDirectory() as directory:
            settings = test_settings(
                report_dir=directory,
                report_backend="smtp",
                smtp_host="smtp.internal.internal.example.invalid",
                smtp_to=["developer@example.com"],
            )
            with mock.patch("smtplib.SMTP", side_effect=socket.gaierror("имя не разрешается")):
                with self.assertLogs("app.services.reporting", level="ERROR"):
                    delivery = ReportService(settings).send(_report())

        self.assertFalse(delivery.sent)
        self.assertIn("smtp.internal.internal.example.invalid", delivery.reason)
        self.assertIsNotNone(delivery.path)

    def test_ssl_backend_is_used_when_requested(self):
        with TemporaryDirectory() as directory:
            settings = test_settings(
                report_dir=directory,
                report_backend="smtp",
                smtp_host="mail.local",
                smtp_to=["a@b.c"],
                smtp_use_ssl=True,
            )
            with mock.patch("smtplib.SMTP_SSL") as ssl_class:
                ReportService(settings).send(_report())

        ssl_class.assert_called_once()


class AttachmentTests(NetworkGuardTestCase):
    def test_oversized_attachment_is_explained_in_the_letter(self):
        """Слишком большой файл — не повод молчать.

        Имя у теста было правильное и раньше, а проверял он только факт
        отправки. Между тем письмо уходило без файла и без единого намёка
        почему: получатель видел отчёт-обещание и пустое вложение.
        """
        with TemporaryDirectory() as directory:
            settings = test_settings(
                report_dir=directory,
                report_backend="smtp",
                smtp_host="mail.local",
                smtp_to=["a@b.c"],
                smtp_max_attachment_mb=1,
            )
            service = ReportService(settings)
            original_stat = Path.stat
            with mock.patch("smtplib.SMTP") as smtp_class, mock.patch.object(
                Path, "stat", autospec=True, side_effect=lambda path, *a, **k: os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 50 * 1024 * 1024, 0, 0, 0)) if path.suffix == ".xlsx" else original_stat(path, *a, **k)
            ):
                delivery = service.send(_report())

        self.assertTrue(delivery.sent)
        message = smtp_class.return_value.__enter__.return_value.send_message.call_args[0][0]
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("не вложен", body)
        self.assertIn("SMTP_MAX_ATTACHMENT_MB=1", body)
        # Файла в письме действительно нет — иначе объяснение было бы вторым враньём.
        self.assertEqual([], [part.get_filename() for part in message.iter_attachments()])


class EmailBodyTests(NetworkGuardTestCase):
    def test_body_contains_group_breakdown(self):
        """Группировка есть в файле отчёта — в письме её не было."""
        body = ReportService(test_settings())._build_text(_report())

        self.assertIn("Проверено, запись не выполнялась", body)
        self.assertIn("Требуется ручная обработка", body)

    def test_empty_run_is_stated_explicitly(self):
        body = ReportService(test_settings())._build_text(
            _report(results=[], total_saby_documents=0)
        )

        self.assertIn("документов не обработано", body)

    def test_body_states_effective_mode(self):
        body = ReportService(test_settings())._build_text(_report())

        self.assertIn("Тестовый режим: Да", body)
        self.assertIn("запрещена настройкой", body)


if __name__ == "__main__":
    unittest.main()
