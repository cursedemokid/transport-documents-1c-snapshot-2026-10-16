import unittest
from datetime import date, datetime
from decimal import Decimal

from app.utils.one_s_format import (
    format_one_s_period,
    parse_document_presentation,
    parse_one_s_amount,
    parse_one_s_bool,
    parse_one_s_date,
    parse_one_s_datetime,
    parse_one_s_text,
)
from tests.support.no_network import NetworkGuardTestCase

NBSP = " "


class AmountTests(NetworkGuardTestCase):
    def test_parses_non_breaking_space_and_comma(self):
        self.assertEqual(parse_one_s_amount(f"1{NBSP}191,06"), Decimal("1191.06"))
        self.assertEqual(parse_one_s_amount(f"193{NBSP}408,42"), Decimal("193408.42"))

    def test_parses_integer_without_fraction(self):
        self.assertEqual(parse_one_s_amount(f"10{NBSP}000"), Decimal("10000"))
        self.assertEqual(parse_one_s_amount("56"), Decimal("56"))

    def test_empty_is_none(self):
        self.assertIsNone(parse_one_s_amount(""))
        self.assertIsNone(parse_one_s_amount(None))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_one_s_amount("не сумма")


class DateTests(NetworkGuardTestCase):
    def test_parses_single_digit_hour(self):
        """1С не дополняет час нулём: '06.08.2026 0:00:00'."""
        self.assertEqual(parse_one_s_date("06.08.2026 0:00:00"), date(2026, 8, 6))
        self.assertEqual(
            parse_one_s_datetime("01.08.2026 23:59:59"),
            datetime(2026, 8, 1, 23, 59, 59),
        )

    def test_sentinel_date_is_none(self):
        """01.01.0001 в 1С означает пустую дату."""
        self.assertIsNone(parse_one_s_date("01.01.0001 0:00:00"))
        self.assertIsNone(parse_one_s_datetime("01.01.0001 0:00:00"))

    def test_empty_is_none(self):
        self.assertIsNone(parse_one_s_date(""))
        self.assertIsNone(parse_one_s_date(None))

    def test_date_only_string(self):
        self.assertEqual(parse_one_s_date("16.04.2026"), date(2026, 4, 16))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_one_s_date("2026-04-16")

    def test_format_period(self):
        self.assertEqual(format_one_s_period(date(2026, 8, 1)), "20260801")


class BoolTests(NetworkGuardTestCase):
    def test_parses_russian_words(self):
        self.assertIs(parse_one_s_bool("Да"), True)
        self.assertIs(parse_one_s_bool("Нет"), False)
        self.assertIs(parse_one_s_bool("нет"), False)

    def test_passes_through_real_bool(self):
        self.assertIs(parse_one_s_bool(True), True)
        self.assertIs(parse_one_s_bool(False), False)

    def test_empty_is_none(self):
        self.assertIsNone(parse_one_s_bool(""))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_one_s_bool("возможно")


class TextTests(NetworkGuardTestCase):
    def test_empty_string_is_none(self):
        self.assertIsNone(parse_one_s_text(""))
        self.assertIsNone(parse_one_s_text("   "))
        self.assertEqual(parse_one_s_text("  ЖД РФ "), "ЖД РФ")


class PresentationTests(NetworkGuardTestCase):
    def test_parses_supplier_order(self):
        parsed = parse_document_presentation("Заказ поставщику ТД000001075 от 16.04.2026 0:00:00")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.kind, "Заказ поставщику")
        self.assertEqual(parsed.number, "ТД000001075")
        self.assertEqual(parsed.date, date(2026, 4, 16))

    def test_parses_kind_with_parentheses(self):
        """Вид документа может содержать скобки — номер всё равно разбирается верно."""
        parsed = parse_document_presentation(
            "Заказ поставщику (спецификация) 1938870207 от 15.04.2026 0:00:00"
        )

        self.assertEqual(parsed.kind, "Заказ поставщику (спецификация)")
        self.assertEqual(parsed.number, "1938870207")
        self.assertEqual(parsed.date, date(2026, 4, 15))

    def test_keeps_raw_string(self):
        raw = "Заказ поставщику ТД000001075 от 16.04.2026 0:00:00"

        self.assertEqual(parse_document_presentation(raw).raw, raw)

    def test_unparsable_is_none(self):
        self.assertIsNone(parse_document_presentation(""))
        self.assertIsNone(parse_document_presentation(None))
        self.assertIsNone(parse_document_presentation("Заказ поставщику ТД000001075"))
        self.assertIsNone(parse_document_presentation("мусор"))


if __name__ == "__main__":
    unittest.main()
