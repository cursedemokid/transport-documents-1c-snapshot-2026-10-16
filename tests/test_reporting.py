import os
import unittest
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory

from app.schemas.sync import SyncItemResult, SyncRunResponse
from app.services.report_file import (
    HEADERS,
    MAX_CELL_LENGTH,
    REPORT_FILE_PREFIX,
    _row,
    prune_reports,
    save_report_file,
)
from app.services.reporting import ReportService
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import test_settings


def _response(results: list[SyncItemResult], **overrides) -> SyncRunResponse:
    params = {
        "request_id": "test-report",
        "dry_run": False,
        "writes_enabled": True,
        "date_from": date(2026, 8, 17),
        "date_to": date(2026, 8, 17),
        "total_saby_documents": len(results),
        "results": results,
    }
    params.update(overrides)
    return SyncRunResponse(**params)


class ReportRowTests(NetworkGuardTestCase):
    def test_russian_headers_statuses_and_attempts(self):
        item = SyncItemResult(
            status="written",
            attempt=2,
            saby_id="saby-1",
            saby_number="4726385",
            saby_date=date(2026, 8, 17),
            counterparty_name="ВЕКТОР ООО",
            amount=Decimal("100.00"),
            additional_expense_number="ТД000000840",
            supplier_order_number="ТД000001405",
            warehouse_group="ОСНОВНЫЕ",
            additional_expense_type="ЖД РФ",
            message="Тест",
        )

        row = _row(item)

        self.assertIn("Статус", HEADERS)
        self.assertIn("Попытка", HEADERS)
        self.assertEqual("Записано", row[0])
        self.assertEqual("2", row[1])
        self.assertNotIn("written", row)


class ReportFileTests(NetworkGuardTestCase):
    def test_file_includes_success_and_error_results(self):
        report = _response(
            [
                SyncItemResult(
                    status="written",
                    saby_number="4726385",
                    saby_date=date(2026, 8, 17),
                    amount=Decimal("100.00"),
                    message="Успешно",
                ),
                SyncItemResult(
                    status="business_error",
                    saby_number="4726386",
                    saby_date=date(2026, 8, 17),
                    amount=Decimal("200.00"),
                    message="Ошибка",
                ),
            ]
        )

        with TemporaryDirectory() as directory:
            path = save_report_file(report, directory, "csv")
            text = path.read_text(encoding="utf-8-sig")

        self.assertIn("Записано", text)
        self.assertIn("Ошибка проверки", text)
        self.assertIn("4726385", text)
        self.assertIn("4726386", text)

    def test_header_states_whether_writes_were_enabled(self):
        """По отчёту должно быть видно, был ли прогон боевым."""
        report = _response([], dry_run=True, writes_enabled=False)

        with TemporaryDirectory() as directory:
            path = save_report_file(report, directory, "csv")
            text = path.read_text(encoding="utf-8-sig")

        self.assertIn("Запись в 1С", text)
        self.assertIn("запрещена", text)


class XlsxFileTests(NetworkGuardTestCase):
    """Валидность книги проверяется разбором, а не глазами.

    Excel на невалидный XML отвечает отказом открыть файл целиком, и узнать об
    этом можно было только от получателя отчёта — на следующее утро.
    """

    def _sheet_text(self, report) -> str:
        with TemporaryDirectory() as directory:
            path = save_report_file(report, directory, "xlsx")
            self.assertEqual(".xlsx", path.suffix)
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                parts = archive.namelist()
                for name in parts:
                    # Невалидна любая часть — книга не откроется.
                    ET.fromstring(archive.read(name))
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        for required in (
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
        ):
            self.assertIn(required, parts)
        return sheet

    def test_plain_report_is_a_valid_workbook(self):
        report = _response(
            [
                SyncItemResult(
                    status="written",
                    saby_number="4726385",
                    amount=Decimal("100.00"),
                    message="Успешно",
                )
            ]
        )

        sheet = self._sheet_text(report)

        self.assertIn("4726385", sheet)

    def test_control_characters_from_1c_exception_do_not_break_the_file(self):
        """Регрессия: \x07 из текста исключения 1С делал книгу невалидной."""
        report = _response(
            [SyncItemResult(status="technical_error", message="Ошибка 1С:\x07 сбой\x0b строки")]
        )

        sheet = self._sheet_text(report)

        self.assertNotIn("\x07", sheet)
        self.assertIn("Ошибка 1С", sheet)

    def test_markup_and_ampersands_are_escaped(self):
        report = _response(
            [SyncItemResult(status="technical_error", message='ООО «Пример» <не найден> \'кавычка\'')]
        )

        sheet = self._sheet_text(report)

        self.assertIn("&amp;", sheet)
        self.assertIn("&lt;", sheet)

    def test_leading_spaces_survive(self):
        report = _response([SyncItemResult(status="manual", message="   с отступом")])

        sheet = self._sheet_text(report)

        self.assertIn('xml:space="preserve"', sheet)
        self.assertIn("   с отступом", sheet)

    def test_overlong_text_is_truncated_to_excel_limit(self):
        report = _response([SyncItemResult(status="manual", message="я" * (MAX_CELL_LENGTH + 500))])

        sheet = self._sheet_text(report)

        cells = [chunk.split("</t>")[0] for chunk in sheet.split('<t xml:space="preserve">')[1:]]
        longest = max(cells, key=len)
        self.assertEqual(MAX_CELL_LENGTH, len(longest))
        self.assertTrue(longest.endswith("…"))


class PruneTests(NetworkGuardTestCase):
    """Каталог отчётов растёт на файл за прогон и никем не чистился."""

    def _directory(self, names):
        directory = self.enterContext(TemporaryDirectory())
        for index, name in enumerate(names):
            path = Path(directory) / name
            path.write_text("x", encoding="utf-8")
            os.utime(path, (index, index))
        return directory

    def test_only_the_newest_files_survive(self):
        directory = self._directory([f"{REPORT_FILE_PREFIX}{i}.csv" for i in range(5)])

        prune_reports(directory, keep=2)

        self.assertEqual(
            {f"{REPORT_FILE_PREFIX}3.csv", f"{REPORT_FILE_PREFIX}4.csv"},
            {path.name for path in Path(directory).iterdir()},
        )

    def test_foreign_files_are_never_touched(self):
        directory = self._directory(
            [f"{REPORT_FILE_PREFIX}{i}.csv" for i in range(3)] + ["важное.xlsx", "README.md"]
        )

        prune_reports(directory, keep=1)

        survivors = {path.name for path in Path(directory).iterdir()}
        self.assertIn("важное.xlsx", survivors)
        self.assertIn("README.md", survivors)

    def test_zero_disables_rotation(self):
        directory = self._directory([f"{REPORT_FILE_PREFIX}{i}.csv" for i in range(3)])

        self.assertEqual([], prune_reports(directory, keep=0))
        self.assertEqual(3, len(list(Path(directory).iterdir())))

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual([], prune_reports("нет-такого-каталога", keep=5))

    def test_saving_a_report_prunes_the_directory(self):
        with TemporaryDirectory() as directory:
            for index in range(4):
                path = Path(directory) / f"{REPORT_FILE_PREFIX}old{index}.csv"
                path.write_text("x", encoding="utf-8")
                os.utime(path, (index, index))
            settings = test_settings(
                report_dir=directory, report_format="csv", report_keep_files=2
            )

            saved = ReportService(settings).save(_response([]))

            files = {path.name for path in Path(directory).iterdir()}

        self.assertIn(saved.name, files)
        self.assertEqual(2, len(files))


class TestMessageTests(NetworkGuardTestCase):
    """Проверочное письмо идёт тем же путём, что и отчёт.

    Раньше его слал отдельный скрипт со своей копией разбора настроек, и копия
    расходилась с боевым путём: `smtp+console` она резолвила в `both` и письмо
    отправляла, а продуктив — в `console` и не отправлял.
    """

    def test_console_backend_prints_and_reports_delivery(self):
        service = ReportService(test_settings(report_backend="console", smtp_to=["a@b.c"]))

        delivery = service.send_test_message()

        self.assertTrue(delivery.sent)
        self.assertIsNone(delivery.path)

    def test_none_backend_reports_not_sent(self):
        delivery = ReportService(test_settings(report_backend="none")).send_test_message()

        self.assertFalse(delivery.sent)
        self.assertIn("none", delivery.reason)

    def test_explicit_recipient_overrides_settings(self):
        sent = {}

        class _Service(ReportService):
            def _send_smtp(self, message, recipients):
                sent["to"] = recipients
                sent["subject"] = message["Subject"]

        service = _Service(
            test_settings(report_backend="smtp", smtp_host="mail.local", smtp_to=["отчёт@b.c"])
        )

        delivery = service.send_test_message(["проверка@b.c"])

        self.assertTrue(delivery.sent)
        self.assertEqual(["проверка@b.c"], sent["to"])
        self.assertIn("проверка доставки", sent["subject"])

    def test_unconfigured_smtp_is_reported_not_raised(self):
        service = ReportService(test_settings(report_backend="smtp", smtp_host=None, smtp_to=[]))

        delivery = service.send_test_message()

        self.assertFalse(delivery.sent)
        self.assertIn("SMTP_HOST", delivery.reason)


class CodeLetterTests(NetworkGuardTestCase):
    """Письмо с кодом обязано совпадать с поведением кода.

    Оба письма — и отдельное с кодом, и раздел в отчёте — писали получателю:
    «код не сгорает при вводе, если страница закроется, введите его снова». Код
    при этом гаснет при первом же входе. Человек выполнял прямую инструкцию из
    письма и получал отказ.
    """

    def _text(self, method: str, *args) -> str:
        captured = {}

        class _Service(ReportService):
            def _send_smtp(self, message, recipients):
                captured["text"] = message.get_body(
                    preferencelist=("plain",)
                ).get_content()

        with TemporaryDirectory() as directory:
            service = _Service(test_settings(
                report_backend="smtp", smtp_host="mail.local", smtp_to=["a@b.c"],
                confirm_base_url="http://internal.internal.example.invalid", report_dir=directory,
            ))
            getattr(service, method)(*args)
        return captured["text"]

    def test_code_letter_does_not_promise_a_second_use(self):
        text = self._text("send_confirmation_code", "ABCDEFGHJK", ["a@b.c"])

        self.assertIn("ABCDEFGHJK", text)
        self.assertIn("один раз", text)
        # Ровно та инструкция, которой человек следовал и упирался в отказ.
        self.assertNotIn("введите его снова", text)
        self.assertNotIn("не сгорает", text)

    def test_report_letter_says_the_same(self):
        report = _response([SyncItemResult(status="dry_run_ready", message="ок")],
                           confirmation_code="ABCDEFGHJK")

        text = self._text("send", report)

        self.assertIn("один раз", text)
        self.assertNotIn("введите его снова", text)
        self.assertNotIn("не сгорает", text)


class ConsoleReportTests(NetworkGuardTestCase):
    def test_console_backend_handles_attachment(self):
        message = EmailMessage()
        message["Subject"] = "Тест"
        message["From"] = "developer@example.com"
        message["To"] = "developer@example.com"
        message.set_content("Текст отчета")
        message.add_attachment(b"test", maintype="text", subtype="csv", filename="report.csv")

        ReportService(test_settings())._send_console(message, Path("report.csv"))


if __name__ == "__main__":
    unittest.main()
