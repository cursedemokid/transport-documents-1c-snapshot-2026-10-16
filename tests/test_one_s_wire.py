import unittest
from datetime import date, datetime
from decimal import Decimal

from app.schemas.one_s_wire import (
    AdditionalExpenseQuery,
    AdditionalExpenseRow,
    AdditionalExpenseTypeRow,
    OrganizationRow,
    SupplierOrderQuery,
    SupplierOrderRow,
    UpdateIncomingRequisitesRequest,
    WarehouseRow,
    compact_one_s_payload,
)
from tests.support.fixtures import load_one_s_rows
from tests.support.no_network import NetworkGuardTestCase


class WireRowTests(NetworkGuardTestCase):
    def test_additional_expense_rows_validate(self):
        rows = [AdditionalExpenseRow.model_validate(row) for row in load_one_s_rows("docaddexpenses_2026-08")]
        by_number = {row.number: row for row in rows}

        filled = by_number["ТД000000822"]
        self.assertEqual(filled.document_id, "7e9061fe-d89a-55c4-a8c8-495cf7b0050b")
        self.assertEqual(filled.amount, Decimal("25498"))
        self.assertEqual(filled.date, datetime(2026, 8, 6, 23, 59, 59))
        self.assertIs(filled.posted, True)
        self.assertIs(filled.deletion_mark, False)
        self.assertEqual(filled.incoming_number, "72213")
        self.assertEqual(filled.incoming_date, date(2026, 8, 6))

    def test_empty_requisites_become_none(self):
        rows = {
            row["Номер"]: AdditionalExpenseRow.model_validate(row)
            for row in load_one_s_rows("docaddexpenses_2026-08")
        }

        empty = rows["ТД000000840"]
        self.assertIsNone(empty.incoming_number)
        self.assertIsNone(empty.incoming_date)
        self.assertEqual(empty.amount, Decimal("1191.06"))

    def test_supplier_order_rows_validate(self):
        rows = {
            row["Номер"]: SupplierOrderRow.model_validate(row)
            for row in load_one_s_rows("ordertosupplier_2026-04")
        }

        order = rows["ТД000001075"]
        self.assertEqual(order.warehouse, "Центральный ОСНОВНОЙ")
        self.assertEqual(order.amount, Decimal("193408.42"))
        self.assertEqual(order.departure_month, date(2026, 6, 1))
        self.assertIs(order.additional_expense_flag, True)

    def test_auto_pickup_field_is_absent_for_now(self):
        """1С это поле не отдаёт, и его имя нам пока не назвали."""
        rows = [SupplierOrderRow.model_validate(row) for row in load_one_s_rows("ordertosupplier_2026-04")]

        self.assertTrue(all(row.auto_pickup_fact_date(["ДатаАвтовывоза"]) is None for row in rows))

    def test_auto_pickup_field_is_read_by_configured_name(self):
        """Имя поля приходит из настройки: угадывать его в коде нельзя."""
        row = SupplierOrderRow.model_validate({"Номер": "ДЖ1", "ЛюбоеИмя": "17.08.2026 0:00:00"})

        self.assertEqual(row.auto_pickup_fact_date(["ЛюбоеИмя"]), date(2026, 8, 17))

    def test_first_matching_candidate_wins(self):
        row = SupplierOrderRow.model_validate({"Номер": "ДЖ1", "Второе": "18.08.2026 0:00:00"})

        self.assertEqual(row.auto_pickup_fact_date(["Первое", "Второе"]), date(2026, 8, 18))

    def test_without_configured_name_nothing_is_guessed(self):
        row = SupplierOrderRow.model_validate({"Номер": "ДЖ1", "ДатаАвтовывоза": "17.08.2026 0:00:00"})

        self.assertIsNone(row.auto_pickup_fact_date([]))

    def test_reference_rows_validate(self):
        warehouses = {row.name: row for row in map(WarehouseRow.model_validate, load_one_s_rows("warehouse"))}
        types = {row.name: row for row in map(AdditionalExpenseTypeRow.model_validate, load_one_s_rows("addexpensetype"))}
        organizations = [OrganizationRow.model_validate(row) for row in load_one_s_rows("organization")]

        self.assertEqual(warehouses["Центральный ОСНОВНОЙ"].group_name, "ОСНОВНЫЕ")
        self.assertEqual(types["Фрахт"].code, "000000005")
        self.assertTrue(all(row.code for row in organizations))

    def test_unknown_keys_are_ignored(self):
        row = AdditionalExpenseRow.model_validate({"Номер": "ДЖ1", "НовоеПоле1С": "значение"})

        self.assertEqual(row.number, "ДЖ1")


class PayloadTests(NetworkGuardTestCase):
    def test_compact_drops_empty_but_keeps_false(self):
        payload = compact_one_s_payload(
            {"confirmed": True, "deletemark": False, "organization": "", "ordertosupplier": None}
        )

        self.assertEqual(payload, {"confirmed": True, "deletemark": False})

    def test_additional_expense_payload(self):
        query = AdditionalExpenseQuery(period_begin=date(2026, 8, 1), period_end=date(2026, 8, 31))

        self.assertEqual(
            query.to_payload(),
            {"periodBegin": "20260801", "periodEnd": "20260831", "confirmed": True, "deletemark": False},
        )

    def test_empty_supplier_order_id_is_never_sent(self):
        """Пустой UUID роняет 1С в HTTP 500 — параметр обязан исчезнуть из тела."""
        query = AdditionalExpenseQuery(
            period_begin=date(2026, 8, 1), period_end=date(2026, 8, 31), supplier_order_id=""
        )

        self.assertNotIn("ordertosupplier", query.to_payload())

    def test_supplier_order_id_is_sent_when_set(self):
        query = AdditionalExpenseQuery(
            period_begin=date(2026, 1, 1),
            period_end=date(2027, 1, 1),
            supplier_order_id="7e9061fe-d89a-55c4-a8c8-495cf7b0050b",
        )

        self.assertEqual(query.to_payload()["ordertosupplier"], "7e9061fe-d89a-55c4-a8c8-495cf7b0050b")

    def test_codes_are_sent_as_is(self):
        query = AdditionalExpenseQuery(
            period_begin=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            organization_code="000000001",
            additional_expense_type_code="000000005",
        )
        payload = query.to_payload()

        self.assertEqual(payload["organization"], "000000001")
        self.assertEqual(payload["addexpensetype"], "000000005")

    def test_supplier_order_query_payload(self):
        query = SupplierOrderQuery(period_begin=date(2026, 4, 10), period_end=date(2026, 4, 20))

        self.assertEqual(
            query.to_payload(),
            {"periodBegin": "20260410", "periodEnd": "20260420", "confirmed": True, "deletemark": False},
        )

    def test_update_request_payload(self):
        request = UpdateIncomingRequisitesRequest(
            document_id="7e9061fe-d89a-55c4-a8c8-495cf7b0050b",
            incoming_number="4726/385",
            incoming_date=date(2026, 8, 16),
        )

        self.assertEqual(
            request.to_payload(),
            {
                "docID": "7e9061fe-d89a-55c4-a8c8-495cf7b0050b",
                # Номер уходит без нормализации: слеш в нём значим.
                "incomingdocnumber": "4726/385",
                "incomingdocdate": "20260816",
            },
        )


if __name__ == "__main__":
    unittest.main()
