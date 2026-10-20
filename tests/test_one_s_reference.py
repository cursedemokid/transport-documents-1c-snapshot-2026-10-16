import unittest

from app.clients.http_json import HttpClientError
from app.clients.one_s import unwrap_one_s_response
from app.schemas.one_s_wire import AdditionalExpenseTypeRow, OrganizationRow, WarehouseRow
from app.services.one_s_reference import OneSReferenceCache
from tests.support.fixtures import load_fixture, load_one_s_rows
from tests.support.no_network import NetworkGuardTestCase


class _ReferenceClientStub:
    """Считает обращения, чтобы проверить ленивость и однократность загрузки."""

    def __init__(self):
        self.organization_calls = 0
        self.expense_type_calls = 0
        self.warehouse_calls = 0
        self.counterparty_calls = 0

    def list_organizations(self):
        self.organization_calls += 1
        return [OrganizationRow.model_validate(row) for row in load_one_s_rows("organization")]

    def list_additional_expense_types(self):
        self.expense_type_calls += 1
        return [AdditionalExpenseTypeRow.model_validate(row) for row in load_one_s_rows("addexpensetype")]

    def list_warehouses(self):
        self.warehouse_calls += 1
        return [WarehouseRow.model_validate(row) for row in load_one_s_rows("warehouse")]

    def list_counterparties(self):
        self.counterparty_calls += 1
        return []


class _FailingReferenceClient(_ReferenceClientStub):
    """1С, недоступная весь прогон."""

    def list_warehouses(self):
        self.warehouse_calls += 1
        raise HttpClientError("1С недоступна")


class ReferenceFailureTests(NetworkGuardTestCase):
    """Отказ справочника запоминается наравне с успехом.

    Иначе при недоступной 1С каждый документ дёргал справочник заново: на
    контрольном прогоне отказ растянулся на четверть часа вместо секунд.
    """

    def test_failure_is_requested_only_once(self):
        client = _FailingReferenceClient()
        cache = OneSReferenceCache(client)

        for _ in range(20):
            with self.assertRaises(HttpClientError):
                cache.warehouse_group("Центральный ОСНОВНОЙ")

        self.assertEqual(1, client.warehouse_calls)

    def test_failure_is_raised_again_not_swallowed(self):
        """Тихий None увёл бы документ в «ручную обработку» вместо сбоя 1С."""
        cache = OneSReferenceCache(_FailingReferenceClient())

        with self.assertRaises(HttpClientError) as caught:
            cache.warehouse_group("Центральный ОСНОВНОЙ")

        self.assertIn("1С недоступна", str(caught.exception))

    def test_other_directories_are_still_read(self):
        client = _FailingReferenceClient()
        cache = OneSReferenceCache(client)

        with self.assertRaises(HttpClientError):
            cache.warehouse_group("Центральный ОСНОВНОЙ")

        self.assertIsNone(cache.organization_code("нет такой"))
        self.assertEqual(1, client.organization_calls)


class ReferenceCacheTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        self.client = _ReferenceClientStub()
        self.cache = OneSReferenceCache(self.client)

    def test_warehouse_group_resolves(self):
        self.assertEqual(self.cache.warehouse_group("Центральный ОСНОВНОЙ"), "ОСНОВНЫЕ")
        self.assertEqual(self.cache.warehouse_group("Центральный рекламация"), "технические")

    def test_warehouse_lookup_ignores_case_and_spacing(self):
        self.assertEqual(self.cache.warehouse_group("  центральный   ОСНОВНОЙ "), "ОСНОВНЫЕ")

    def test_unknown_warehouse_is_none(self):
        self.assertIsNone(self.cache.warehouse_group("Склад, которого нет"))
        self.assertIsNone(self.cache.warehouse_group(None))

    def test_expense_type_codes(self):
        self.assertEqual(self.cache.expense_type_code("Фрахт"), "000000005")
        self.assertEqual(self.cache.expense_type_code("ЖД РФ"), "000000008")

    def test_ambiguous_organization_yields_none(self):
        """«прочее» встречается с разными кодами — угадывать нельзя."""
        self.assertIsNone(self.cache.organization_code("прочее"))

    def test_unambiguous_organization_resolves(self):
        self.assertEqual(self.cache.organization_code('ООО «Пример»'), "000000001")

    def test_each_reference_is_loaded_once(self):
        for _ in range(3):
            self.cache.warehouse_group("Центральный ОСНОВНОЙ")
            self.cache.expense_type_code("Фрахт")
            self.cache.organization_code('ООО «Пример»')

        self.assertEqual(self.client.warehouse_calls, 1)
        self.assertEqual(self.client.expense_type_calls, 1)
        self.assertEqual(self.client.organization_calls, 1)

    def test_references_are_loaded_lazily(self):
        self.cache.warehouse_group("Центральный ОСНОВНОЙ")

        self.assertEqual(self.client.organization_calls, 0)
        self.assertEqual(self.client.expense_type_calls, 0)

    def test_counterparty_reference_is_never_touched(self):
        """26798 записей без ИНН не нужны: наименование приходит в самом документе."""
        self.cache.warehouse_group("Центральный ОСНОВНОЙ")
        self.cache.expense_type_code("Фрахт")
        self.cache.organization_code('ООО «Пример»')

        self.assertEqual(self.client.counterparty_calls, 0)


class UnwrapResponseTests(NetworkGuardTestCase):
    def test_unwraps_successful_payload(self):
        payload = load_fixture("one_s/warehouse.json")

        rows = unwrap_one_s_response("/data1C/warehouse", payload)

        self.assertEqual(len(rows), len(payload["data"]))

    def test_error_payload_raises_with_one_s_text(self):
        payload = load_fixture("one_s/error_empty_uuid.json")

        with self.assertRaises(HttpClientError) as caught:
            unwrap_one_s_response("/data1C/docaddexpenses", payload)

        self.assertEqual(caught.exception.status_code, 500)
        self.assertIn("УникальныйИдентификатор", str(caught.exception))

    def test_routing_error_payload_uses_error_key(self):
        """Третья форма ответа: текст ошибки лежит в error, а не в data."""
        payload = load_fixture("one_s/error_bad_path.json")

        with self.assertRaises(HttpClientError) as caught:
            unwrap_one_s_response("/data1C/updatedocaddexpenses", payload)

        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("Некорректный путь", str(caught.exception))

    def test_null_data_is_empty_list(self):
        self.assertEqual(unwrap_one_s_response("/x", {"code": 200, "data": None}), [])

    def test_string_code_is_accepted(self):
        self.assertEqual(unwrap_one_s_response("/x", {"code": "200", "data": []}), [])

    def test_non_dict_response_raises(self):
        with self.assertRaises(HttpClientError):
            unwrap_one_s_response("/x", [1, 2, 3])

    def test_unexpected_data_type_raises(self):
        with self.assertRaises(HttpClientError):
            unwrap_one_s_response("/x", {"code": 200, "data": 42})


if __name__ == "__main__":
    unittest.main()
