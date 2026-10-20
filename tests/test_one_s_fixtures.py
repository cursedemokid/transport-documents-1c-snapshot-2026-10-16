"""Проверяет, что парсеры форматов 1С разбирают реальную форму ответов обмена."""

import unittest
from datetime import date
from decimal import Decimal

from app.utils.normalization import party_name_similarity
from app.utils.one_s_format import (
    parse_document_presentation,
    parse_one_s_amount,
    parse_one_s_bool,
    parse_one_s_date,
    parse_one_s_text,
)
from tests.support.fixtures import load_fixture, load_one_s_rows
from tests.support.no_network import NetworkGuardTestCase


class AdditionalExpenseFixtureTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        self.rows = {row["Номер"]: row for row in load_one_s_rows("docaddexpenses_2026-08")}

    def test_every_row_parses(self):
        for number, row in self.rows.items():
            with self.subTest(number=number):
                self.assertIsNotNone(parse_one_s_date(row["Дата"]))
                self.assertIsNotNone(parse_one_s_amount(row["СуммаДокумента"]))
                self.assertIs(parse_one_s_bool(row["Проведен"]), True)
                self.assertIs(parse_one_s_bool(row["ПометкаУдаления"]), False)

    def test_amount_with_non_breaking_space(self):
        row = self.rows["ТД000000822"]

        self.assertEqual(parse_one_s_amount(row["СуммаДокумента"]), Decimal("25498"))

    def test_empty_incoming_requisites(self):
        """Незаполненные реквизиты приходят как пустой номер и 01.01.0001."""
        row = self.rows["ТД000000840"]

        self.assertIsNone(parse_one_s_text(row["НомерВходящегоДокумента"]))
        self.assertIsNone(parse_one_s_date(row["ДатаВходящегоДокумента"]))

    def test_filled_incoming_requisites(self):
        row = self.rows["ТД000000822"]

        self.assertEqual(parse_one_s_text(row["НомерВходящегоДокумента"]), "72213")
        self.assertEqual(parse_one_s_date(row["ДатаВходящегоДокумента"]), date(2026, 8, 6))

    def test_supplier_order_presentation_resolves_to_number_and_date(self):
        parsed = parse_document_presentation(self.rows["ТД000000822"]["ЗаказПоставщику"])

        self.assertEqual(parsed.number, "ТД000001075")
        self.assertEqual(parsed.date, date(2026, 4, 16))

    def test_missing_supplier_order_is_none(self):
        self.assertIsNone(parse_document_presentation(self.rows["ТД000000841"]["ЗаказПоставщику"]))


class SupplierOrderFixtureTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        self.rows = {row["Номер"]: row for row in load_one_s_rows("ordertosupplier_2026-04")}

    def test_order_referenced_by_expenses_is_present(self):
        order = self.rows["ТД000001075"]

        self.assertEqual(parse_one_s_date(order["Дата"]), date(2026, 4, 16))
        self.assertEqual(order["Склад"], "Центральный ОСНОВНОЙ")

    def test_orders_carry_no_date_rows(self):
        """Ключевой пробел контракта: фактическую дату автовывоза взять неоткуда."""
        for number, order in self.rows.items():
            with self.subTest(number=number):
                self.assertNotIn("ДатыПоКонтейнерам", order)
                self.assertNotIn("Контейнеры", order)
                self.assertFalse([key for key in order if "автовывоз" in key.casefold()])

    def test_end_of_day_timestamp_parses(self):
        self.assertEqual(parse_one_s_date(self.rows["ТД000002206"]["Дата"]), date(2026, 8, 1))


class ReferenceFixtureTests(NetworkGuardTestCase):
    def test_warehouse_group_of_order_warehouse(self):
        groups = {row["Склад"]: row["Группа"] for row in load_one_s_rows("warehouse")}

        self.assertEqual(groups["Центральный ОСНОВНОЙ"], "ОСНОВНЫЕ")
        self.assertEqual(groups["Центральный рекламация"], "технические")

    def test_no_moscow_warehouse_group_exists(self):
        """Константа MOSCOW_WAREHOUSE_GROUP не соответствует ничему в 1С."""
        groups = {row["Группа"] for row in load_one_s_rows("warehouse")}

        self.assertNotIn("Москва", groups)

    def test_expense_type_codes(self):
        codes = {row["ВидДопРасхода"]: row["Код"] for row in load_one_s_rows("addexpensetype")}

        self.assertEqual(codes["Фрахт"], "000000005")
        self.assertEqual(codes["ЖД РФ"], "000000008")

    def test_organization_codes_are_not_unique(self):
        """Маппинг «название организации -> код» принципиально неоднозначен."""
        rows = load_one_s_rows("organization")
        codes = [row["Код"] for row in rows]

        self.assertLess(len(set(codes)), len(codes))

    def test_counterparty_names_need_fuzzy_comparison(self):
        rows = {row["Номер"]: row for row in load_one_s_rows("docaddexpenses_2026-08")}
        dirty = rows["ТД000000822"]["Контрагент"]

        self.assertEqual(dirty, "ВЕКТОР ООО (сверен на 31.12.2025)")
        self.assertEqual(party_name_similarity(dirty, 'ООО «Пример»'), 1.0)


class ErrorFixtureTests(NetworkGuardTestCase):
    def test_error_response_carries_string_instead_of_list(self):
        """При ошибке 1С меняет тип data со списка на строку — клиент обязан это учитывать."""
        payload = load_fixture("one_s/error_empty_uuid.json")

        self.assertEqual(payload["code"], 500)
        self.assertIsInstance(payload["data"], str)
        self.assertIn("УникальныйИдентификатор", payload["data"])


if __name__ == "__main__":
    unittest.main()
