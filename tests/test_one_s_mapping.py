import unittest
from datetime import date, datetime
from decimal import Decimal

from app.schemas.one_s_wire import AdditionalExpenseRow, SupplierOrderRow
from app.services.one_s_mapping import (
    map_additional_expense,
    map_additional_expenses,
    map_supplier_order,
    map_supplier_order_link,
    map_supplier_orders,
)
from tests.support.fixtures import load_one_s_rows
from tests.support.no_network import NetworkGuardTestCase


def _expenses() -> dict[str, object]:
    rows = [AdditionalExpenseRow.model_validate(row) for row in load_one_s_rows("docaddexpenses_2026-08")]
    return {item.number: item for item in map_additional_expenses(rows)}


def _orders() -> dict[str, object]:
    rows = [SupplierOrderRow.model_validate(row) for row in load_one_s_rows("ordertosupplier_2026-04")]
    return {item.number: item for item in map_supplier_orders(rows)}


class AdditionalExpenseMappingTests(NetworkGuardTestCase):
    def test_maps_document_identifier_to_ref(self):
        expense = _expenses()["ТД000000822"]

        self.assertEqual(expense.ref, "7e9061fe-d89a-55c4-a8c8-495cf7b0050b")
        self.assertEqual(expense.amount, Decimal("25498"))
        self.assertEqual(expense.additional_expense_type, "Автовывоз")

    def test_counterparty_has_name_but_no_inn(self):
        expense = _expenses()["ТД000000822"]

        self.assertEqual(expense.counterparty.name, "ВЕКТОР ООО (сверен на 31.12.2025)")
        self.assertIsNone(expense.counterparty.inn)
        self.assertIsNone(expense.organization.inn)

    def test_warehouse_is_empty_because_document_has_none(self):
        expense = _expenses()["ТД000000822"]

        self.assertIsNone(expense.warehouse.name)
        self.assertIsNone(expense.warehouse.group_name)

    def test_incoming_requisites(self):
        expenses = _expenses()

        self.assertEqual(expenses["ТД000000822"].incoming_requisites.number, "72213")
        self.assertEqual(expenses["ТД000000822"].incoming_requisites.date, date(2026, 8, 6))
        self.assertIsNone(expenses["ТД000000840"].incoming_requisites.number)
        self.assertIsNone(expenses["ТД000000840"].incoming_requisites.date)

    def test_row_without_identifier_is_skipped(self):
        rows = [AdditionalExpenseRow.model_validate({"Номер": "ДЖ1", "СуммаДокумента": "10"})]

        self.assertEqual(map_additional_expenses(rows), [])

    def test_single_row_without_identifier_returns_none(self):
        row = AdditionalExpenseRow.model_validate({"Номер": "ДЖ1"})

        self.assertIsNone(map_additional_expense(row))


class SupplierOrderLinkTests(NetworkGuardTestCase):
    def test_parses_number_and_date(self):
        link = map_supplier_order_link("Заказ поставщику ТД000001075 от 16.04.2026 0:00:00")

        self.assertEqual(link.number, "ТД000001075")
        self.assertEqual(link.date, date(2026, 4, 16))
        self.assertEqual(link.presentation, "Заказ поставщику ТД000001075 от 16.04.2026 0:00:00")

    def test_empty_presentation_gives_empty_link(self):
        link = map_supplier_order_link("")

        self.assertIsNone(link.number)
        self.assertIsNone(link.presentation)

    def test_falls_back_to_order_number_regex(self):
        """Если представление изменится, номер всё ещё вытаскивается регуляркой."""
        link = map_supplier_order_link("Заказ поставщику ТД000001075")

        self.assertEqual(link.number, "ТД000001075")
        self.assertIsNone(link.date)
        self.assertEqual(link.presentation, "Заказ поставщику ТД000001075")

    def test_unparsable_presentation_keeps_raw_string(self):
        link = map_supplier_order_link("совсем другое представление")

        self.assertIsNone(link.number)
        self.assertEqual(link.presentation, "совсем другое представление")


class SupplierOrderMappingTests(NetworkGuardTestCase):
    def test_maps_order(self):
        order = _orders()["ТД000001075"]

        self.assertEqual(order.ref, "7e9061fe-d89a-55c4-a8c8-495cf7b0050b")
        self.assertEqual(order.warehouse.name, "Центральный ОСНОВНОЙ")
        self.assertEqual(order.currency, "Юани")

    def test_document_date_keeps_time(self):
        """Время значимо: заказы бывают проведены в 23:59:59, и это влияет на границы периода."""
        orders = _orders()

        self.assertEqual(orders["ТД000001075"].date, datetime(2026, 4, 16, 0, 0, 0))
        self.assertEqual(orders["ТД000002206"].date, datetime(2026, 8, 1, 23, 59, 59))

    def test_warehouse_group_is_resolved_later(self):
        """В заказе нет группы склада — её проставит справочник."""
        order = _orders()["ТД000001075"]

        self.assertIsNone(order.warehouse.group_name)

    def test_no_date_rows_and_no_auto_pickup_fact(self):
        for number, order in _orders().items():
            with self.subTest(number=number):
                self.assertEqual(order.date_rows, [])
                self.assertEqual(order.containers, [])
                self.assertIsNone(order.auto_pickup_fact_date)

    def test_auto_pickup_fact_is_mapped_by_configured_name(self):
        row = SupplierOrderRow.model_validate(
            {"Номер": "ДЖ1", "ИдентификаторДокумента": "uuid-1", "ЛюбоеИмя": "17.08.2026 0:00:00"}
        )

        mapped = map_supplier_order(row, ["ЛюбоеИмя"])

        self.assertEqual(mapped.auto_pickup_fact_date, date(2026, 8, 17))

    def test_auto_pickup_fact_stays_empty_without_configured_name(self):
        """Без подтверждённого имени поля проверка обязана оставаться выключенной."""
        row = SupplierOrderRow.model_validate(
            {"Номер": "ДЖ1", "ИдентификаторДокумента": "uuid-1", "ЛюбоеИмя": "17.08.2026 0:00:00"}
        )

        self.assertIsNone(map_supplier_order(row).auto_pickup_fact_date)

    def test_base_document_presentation_with_parentheses(self):
        order = _orders()["ТД000001075"]

        self.assertEqual(order.base_document.number, "1938870207")
        self.assertEqual(order.base_document.date, date(2026, 4, 15))


if __name__ == "__main__":
    unittest.main()
