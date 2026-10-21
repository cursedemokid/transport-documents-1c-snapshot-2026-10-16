import unittest
from datetime import date

from app.schemas.one_s import ContainerDates, DateRow, IncomingRequisites
from app.services.one_s_repository import OrderLookup
from app.services.validation import (
    ValidationContext,
    resolve_auto_pickup_fact,
    validate_before_write,
)
from app.utils.normalization import clean_text
from tests.support.fakes import expense, order, saby_doc
from tests.support.fixtures import load_one_s_rows
from tests.support.no_network import NetworkGuardTestCase

AUTO_PICKUP_ROW = "Дата автовывоза со станции/порта = Пост."
WAREHOUSE_GROUPS = {row["Склад"]: row["Группа"] for row in load_one_s_rows("warehouse")}


def _context(**overrides) -> ValidationContext:
    params = {
        "allowed_expense_types": frozenset({clean_text("Фрахт"), clean_text("ЖД РФ")}),
        "allowed_warehouse_groups": frozenset({clean_text("ОСНОВНЫЕ")}),
        "warehouse_group_resolver": WAREHOUSE_GROUPS.get,
        "auto_pickup_row_name": AUTO_PICKUP_ROW,
    }
    params.update(overrides)
    return ValidationContext(**params)


def _found(item=None) -> OrderLookup:
    return OrderLookup(status="found", order=item or order())


class HappyPathTests(NetworkGuardTestCase):
    def test_moscow_freight_within_deadline_passes(self):
        result = validate_before_write(saby_doc(), expense(), _found(), _context())

        self.assertIsNone(result)

    def test_same_date_is_allowed(self):
        item = order(auto_pickup_fact_date=date(2026, 8, 17))

        self.assertIsNone(validate_before_write(saby_doc(), expense(), _found(item), _context()))


class DocumentStateTests(NetworkGuardTestCase):
    """Состояние документа проверяется перед самой записью, а не только в выборке.

    Постфильтр выборки отсеивает удалённые и непроведённые документы, но между
    предложением и подтверждением проходят часы: письмо читают не сразу. За это
    время документ успевают пометить на удаление или распровести, а
    перечитывание перед записью вернёт его как ни в чём не бывало — на этом пути
    постфильтра нет.
    """

    def test_deleted_document_is_not_written(self):
        result = validate_before_write(
            saby_doc(), expense(deletion_mark=True), _found(), _context()
        )

        self.assertIsNotNone(result)
        self.assertEqual("manual", result.status)
        self.assertIn("помечен на удаление", result.message)

    def test_unposted_document_is_not_written(self):
        result = validate_before_write(
            saby_doc(), expense(posted=False), _found(), _context()
        )

        self.assertIsNotNone(result)
        self.assertEqual("manual", result.status)
        self.assertIn("не проведён", result.message)

    def test_unknown_posting_state_is_not_a_refusal(self):
        """Обмен не всегда отдаёт признак проведения; пустое — не «распроведён»."""
        self.assertIsNone(
            validate_before_write(saby_doc(), expense(posted=None), _found(), _context())
        )


class IncomingRequisitesTests(NetworkGuardTestCase):
    def test_identical_requisites_are_already_filled(self):
        filled = expense(
            incoming_requisites=IncomingRequisites(number="4726385", date=date(2026, 8, 17))
        )

        result = validate_before_write(saby_doc(), filled, _found(), _context())

        self.assertEqual(result.status, "already_filled")

    def test_zero_padded_saby_number_is_the_same_document(self):
        """Saby отдаёт «3572303003», бухгалтер записал «4726385» — это один УПД.

        На бэкфилле за восемь месяцев так набиралось семь ложных конфликтов из
        девяти, а конфликт — стоп-условие при раскатке записи.
        """
        filled = expense(
            incoming_requisites=IncomingRequisites(number="4726385", date=date(2026, 8, 17))
        )

        result = validate_before_write(
            saby_doc(incoming_number="0004726385"), filled, _found(), _context()
        )

        self.assertEqual(result.status, "already_filled")

    def test_numbers_differing_beyond_padding_are_still_a_conflict(self):
        filled = expense(
            incoming_requisites=IncomingRequisites(number="3300111", date=date(2026, 8, 17))
        )

        result = validate_before_write(
            saby_doc(incoming_number="2200111"), filled, _found(), _context()
        )

        self.assertEqual(result.status, "conflict")

    def test_sentinel_date_counts_as_empty(self):
        """01.01.0001 не должна превращать пустой документ в вечный конфликт."""
        item = expense(incoming_requisites=IncomingRequisites(number=None, date=date(1, 1, 1)))

        self.assertIsNone(validate_before_write(saby_doc(), item, _found(), _context()))

    def test_other_number_is_conflict(self):
        item = expense(
            incoming_requisites=IncomingRequisites(number="OTHER-77", date=date(2026, 8, 1))
        )

        result = validate_before_write(saby_doc(), item, _found(), _context())

        self.assertEqual(result.status, "conflict")
        self.assertIn("OTHER-77", result.message)

    def test_same_number_but_other_date_is_conflict(self):
        item = expense(
            incoming_requisites=IncomingRequisites(number="4726385", date=date(2026, 8, 15))
        )

        result = validate_before_write(saby_doc(), item, _found(), _context())

        self.assertEqual(result.status, "conflict")
        self.assertIn("2026-08-15", result.message)

    def test_partial_requisites_can_be_completed(self):
        """Заполнен только совпадающий номер — дату дозаполняем."""
        item = expense(incoming_requisites=IncomingRequisites(number="4726385", date=None))

        self.assertIsNone(validate_before_write(saby_doc(), item, _found(), _context()))

    def test_partial_completion_can_be_disabled(self):
        item = expense(incoming_requisites=IncomingRequisites(number="4726385", date=None))

        result = validate_before_write(
            saby_doc(), item, _found(), _context(allow_completing_partial=False)
        )

        self.assertEqual(result.status, "conflict")


class SupplierOrderTests(NetworkGuardTestCase):
    def test_missing_order_number_is_manual(self):
        result = validate_before_write(
            saby_doc(), expense(order_number=None), OrderLookup(status="no_number"), _context()
        )

        self.assertEqual(result.status, "manual")

    def test_order_outside_period_is_business_error(self):
        result = validate_before_write(
            saby_doc(), expense(), OrderLookup(status="not_in_period"), _context()
        )

        self.assertEqual(result.status, "business_error")
        self.assertIn("не найден", result.message)

    def test_ambiguous_order_is_manual(self):
        lookup = OrderLookup(status="ambiguous", candidates=(order(), order()))

        result = validate_before_write(saby_doc(), expense(), lookup, _context())

        self.assertEqual(result.status, "manual")


class WarehouseTests(NetworkGuardTestCase):
    def test_allowed_group_passes(self):
        self.assertIsNone(validate_before_write(saby_doc(), expense(), _found(), _context()))

    def test_other_group_is_manual(self):
        item = order(warehouse_name="Центральный рекламация")

        result = validate_before_write(saby_doc(), expense(), _found(item), _context())

        self.assertEqual(result.status, "manual")
        self.assertIn("технические", result.message)

    def test_unknown_warehouse_is_manual(self):
        item = order(warehouse_name="Склад, которого нет в справочнике")

        result = validate_before_write(saby_doc(), expense(), _found(item), _context())

        self.assertEqual(result.status, "manual")
        self.assertIn("не найден в справочнике", result.message)

    def test_resolved_group_reaches_the_report(self):
        item = order(warehouse_name="Центральный рекламация")

        result = validate_before_write(saby_doc(), expense(), _found(item), _context())

        self.assertEqual(result.warehouse_group, "технические")


class AutoPickupTests(NetworkGuardTestCase):
    def test_missing_fact_is_manual_not_error(self):
        """1С не передаёт это поле, поэтому документ идёт в ручную обработку."""
        item = order(auto_pickup_fact_date=None)

        result = validate_before_write(saby_doc(), expense(), _found(item), _context())

        self.assertEqual(result.status, "manual")
        self.assertIn("не передаёт это поле", result.message)

    def test_late_upd_is_business_error(self):
        item = order(auto_pickup_fact_date=date(2026, 8, 16))

        result = validate_before_write(saby_doc(), expense(), _found(item), _context())

        self.assertEqual(result.status, "business_error")
        self.assertIn("позже", result.message)

    def test_flat_field_enables_the_check(self):
        """Как только 1С начнёт отдавать поле, проверка заработает без правок."""
        item = order(auto_pickup_fact_date=date(2026, 8, 20))

        self.assertEqual(resolve_auto_pickup_fact(item, AUTO_PICKUP_ROW), date(2026, 8, 20))

    def test_date_rows_are_used_when_they_appear(self):
        item = order(
            auto_pickup_fact_date=None,
            date_rows=[DateRow(name=AUTO_PICKUP_ROW, plan=date(2026, 8, 1), fact=date(2026, 8, 18))],
        )

        self.assertEqual(resolve_auto_pickup_fact(item, AUTO_PICKUP_ROW), date(2026, 8, 18))

    def test_plan_date_is_never_used(self):
        item = order(
            auto_pickup_fact_date=None,
            date_rows=[DateRow(name=AUTO_PICKUP_ROW, plan=date(2026, 8, 1), fact=None)],
        )

        self.assertIsNone(resolve_auto_pickup_fact(item, AUTO_PICKUP_ROW))

    def test_earliest_container_fact_wins(self):
        item = order(
            auto_pickup_fact_date=None,
            containers=[
                ContainerDates(
                    number="CNT-1",
                    date_rows=[DateRow(name=AUTO_PICKUP_ROW, fact=date(2026, 8, 20))],
                ),
                ContainerDates(
                    number="CNT-2",
                    date_rows=[DateRow(name=AUTO_PICKUP_ROW, fact=date(2026, 8, 16))],
                ),
            ],
        )

        self.assertEqual(resolve_auto_pickup_fact(item, AUTO_PICKUP_ROW), date(2026, 8, 16))


class ExpenseTypeTests(NetworkGuardTestCase):
    def test_unsupported_type_is_manual(self):
        result = validate_before_write(
            saby_doc(), expense(expense_type="Автовывоз"), _found(), _context()
        )

        self.assertEqual(result.status, "manual")
        self.assertIn("Автовывоз", result.message)


if __name__ == "__main__":
    unittest.main()
