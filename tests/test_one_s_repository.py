import unittest
from datetime import date, datetime, timedelta

from app.schemas.one_s import IncomingRequisites
from app.clients.http_json import HttpClientError
from app.schemas.one_s_wire import (
    AdditionalExpenseRow,
    SupplierOrderAttributeRow,
    SupplierOrderRow,
    UpdateIncomingRequisitesResult,
)
from app.services.one_s_repository import OneSRepository
from tests.support.fakes import FakeOneSClient
from tests.support.fixtures import load_one_s_rows
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import test_settings

EXPENSE_ROWS = [AdditionalExpenseRow.model_validate(row) for row in load_one_s_rows("docaddexpenses_2026-08")]
ORDER_ROWS = [SupplierOrderRow.model_validate(row) for row in load_one_s_rows("ordertosupplier_2026-04")]


class _WritingClient(FakeOneSClient):
    """Фиксирует запись и умеет подменять документ после неё."""

    def __init__(self, response=None, after_write=None, **kwargs):
        super().__init__(**kwargs)
        self._response = response if response is not None else {"code": 200}
        self._after_write = after_write
        self.write_calls = 0

    def update_incoming_requisites(self, request):
        self.write_calls += 1
        if self._after_write is not None:
            self.expense_rows = self._after_write
        return UpdateIncomingRequisitesResult.from_response(self._response)


def _repository(client=None, **overrides):
    settings = test_settings(**overrides)
    return OneSRepository(settings, client=client or FakeOneSClient())


def _expense_row_with(**changes) -> list[AdditionalExpenseRow]:
    rows = [row.model_copy() for row in EXPENSE_ROWS]
    for row in rows:
        if row.number == "ТД000000840":
            for key, value in changes.items():
                setattr(row, key, value)
    return rows



class ExpenseMemoizationTests(NetworkGuardTestCase):
    def test_period_is_fetched_once(self):
        client = FakeOneSClient()
        repository = _repository(client)

        for _ in range(5):
            repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))

        self.assertEqual(1, len(client.expense_queries))

    def test_different_periods_are_fetched_separately(self):
        client = FakeOneSClient()
        repository = _repository(client)

        repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        repository.list_additional_expenses(date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(2, len(client.expense_queries))

    def test_deleted_documents_are_filtered_out(self):
        """Защитный постфильтр на случай, если серверные параметры перестанут работать."""
        client = FakeOneSClient(expense_rows=_expense_row_with(deletion_mark=True))
        repository = _repository(client)

        items = repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))

        self.assertNotIn("ТД000000840", [item.number for item in items])

    def test_empty_supplier_order_filter_is_never_sent(self):
        client = FakeOneSClient()
        repository = _repository(client)

        repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))

        self.assertNotIn("ordertosupplier", client.expense_queries[0].to_payload())


class _AlwaysFailingClient(FakeOneSClient):
    """1С, недоступная весь прогон, — а не один раз."""

    def list_additional_expenses(self, query):
        self.expense_queries.append(query)
        raise HttpClientError("1С недоступна")


class ExpenseFailureCacheTests(NetworkGuardTestCase):
    """Отказ массовой выборки запоминается — иначе прогон зависает на часы.

    Таймаут выборки — 120 с, попыток на документ две. Полсотни документов без
    кэша отказа превращают один недоступный сервис в многочасовое ожидание,
    которое всё равно закончится теми же техническими ошибками.
    """

    def test_failure_is_retried_up_to_max_attempts_then_cached(self):
        client = _AlwaysFailingClient()
        repository = _repository(client, sync_max_attempts=2)

        for _ in range(20):
            with self.assertRaises(HttpClientError):
                repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))

        self.assertEqual(2, len(client.expense_queries))

    def test_cached_failure_keeps_the_original_error(self):
        client = _AlwaysFailingClient()
        repository = _repository(client, sync_max_attempts=1)

        with self.assertRaises(HttpClientError):
            repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        with self.assertRaises(HttpClientError) as caught:
            repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))

        self.assertIn("1С недоступна", str(caught.exception))

    def test_other_periods_are_still_requested(self):
        """Кэшируется конкретный период, а не «1С сломалась вообще»."""
        client = _AlwaysFailingClient()
        repository = _repository(client, sync_max_attempts=1)

        with self.assertRaises(HttpClientError):
            repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        with self.assertRaises(HttpClientError):
            repository.list_additional_expenses(date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(2, len(client.expense_queries))

    def test_success_after_a_failure_clears_the_cache(self):
        client = FakeOneSClient(expense_error=HttpClientError("разовый сбой"))
        repository = _repository(client, sync_max_attempts=2)

        with self.assertRaises(HttpClientError):
            repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        items = repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))

        self.assertTrue(items)
        self.assertEqual(2, len(client.expense_queries))


class SupplierOrderLookupTests(NetworkGuardTestCase):
    def _expense(self, number: str = "ТД000001075"):
        items = _repository().list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        item = next(i for i in items if i.supplier_order.number)
        return item.model_copy(
            update={"supplier_order": item.supplier_order.model_copy(update={"number": number})}
        )

    def test_hint_date_closes_lookup_in_one_request(self):
        """Дата из представления заказа делает поиск точечным."""
        client = FakeOneSClient()
        repository = _repository(client)

        lookup = repository.find_supplier_order(self._expense())

        self.assertEqual(lookup.status, "found")
        self.assertEqual(lookup.order.number, "ТД000001075")
        self.assertEqual(1, len(client.order_queries))

    def test_requested_period_covers_hint_date(self):
        client = FakeOneSClient()
        repository = _repository(client)

        repository.find_supplier_order(self._expense())

        query = client.order_queries[0]
        self.assertLessEqual(query.period_begin, date(2026, 4, 16))
        self.assertGreaterEqual(query.period_end, date(2026, 4, 16))

    def test_missing_number_is_reported_separately(self):
        item = self._expense()
        item = item.model_copy(
            update={"supplier_order": item.supplier_order.model_copy(update={"number": None})}
        )

        self.assertEqual(_repository().find_supplier_order(item).status, "no_number")

    def test_unknown_number_exhausts_chunks(self):
        """Лимит ограничивает слепой перебор; точечный запрос по дате идёт сверх него."""
        client = FakeOneSClient(order_rows=[])
        repository = _repository(client, one_s_bulk_max_chunks=3)

        lookup = repository.find_supplier_order(self._expense("ТД999999999"))

        self.assertEqual(lookup.status, "not_in_period")
        self.assertEqual(3, repository._orders.walk_requests)
        self.assertEqual(1, repository._orders.targeted_requests)

    def test_duplicate_numbers_are_disambiguated_by_date(self):
        duplicate = ORDER_ROWS[0].model_copy(
            update={"date": datetime(2026, 4, 18, 0, 0, 0), "document_id": "другой-uuid"}
        )
        client = FakeOneSClient(order_rows=[*ORDER_ROWS, duplicate])
        repository = _repository(client)

        lookup = repository.find_supplier_order(self._expense())

        self.assertEqual(lookup.status, "found")
        self.assertEqual(lookup.order.ref, "7e9061fe-d89a-55c4-a8c8-495cf7b0050b")

    def test_many_targeted_lookups_do_not_exhaust_the_index(self):
        """Регрессия: лимит на перебор периодов не должен гасить точечные запросы.

        Раньше он считал все обращения подряд, и после ONE_S_BULK_MAX_CHUNKS
        документов индекс объявлялся исчерпанным — остальные заказы переставали
        находиться, хотя каждый достаётся одним точным запросом по дате.
        """
        orders = [
            ORDER_ROWS[0].model_copy(
                update={
                    "number": f"ТД00000{index:04d}",
                    "document_id": f"uuid-{index}",
                    "date": datetime(2026, 1, 1) + timedelta(days=index * 10),
                }
            )
            for index in range(12)
        ]
        client = FakeOneSClient(order_rows=orders)
        repository = _repository(client, one_s_bulk_max_chunks=2)
        base = _repository().list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))[0]

        found = 0
        for index, order in enumerate(orders):
            link = base.supplier_order.model_copy(
                update={"number": order.number, "date": (datetime(2026, 1, 1) + timedelta(days=index * 10)).date()}
            )
            lookup = repository.find_supplier_order(base.model_copy(update={"supplier_order": link}))
            if lookup.status == "found":
                found += 1

        self.assertEqual(12, found, "все заказы достаются точечными запросами по дате")

    def test_targeted_lookup_reaches_orders_older_than_sync_start(self):
        """Дата заказа известна точно, поэтому нижняя граница окна тут ни при чём."""
        old_order = ORDER_ROWS[0].model_copy(
            update={"number": "ТД000000198", "document_id": "uuid-old", "date": datetime(2025, 2, 27)}
        )
        client = FakeOneSClient(order_rows=[old_order])
        repository = _repository(client, sync_start_date=date(2026, 1, 1))
        base = _repository().list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))[0]
        link = base.supplier_order.model_copy(
            update={"number": "ТД000000198", "date": date(2025, 2, 27)}
        )

        lookup = repository.find_supplier_order(base.model_copy(update={"supplier_order": link}))

        self.assertEqual(lookup.status, "found")
        self.assertLessEqual(client.order_queries[0].period_begin, date(2025, 2, 27))

    def _lookup_by(self, repository, base, number, day):
        link = base.supplier_order.model_copy(update={"number": number, "date": day})
        return repository.find_supplier_order(base.model_copy(update={"supplier_order": link}))

    def test_old_order_does_not_poison_the_walk(self):
        """Регрессия: точечный запрос за старым заказом уводил перебор в прошлое.

        Периоды точечных запросов попадали в общий список, а перебор брал по нему
        минимум. После обращения за февральским заказом перебор стартовал с
        февраля и назад — свежие периоды не читались никогда, и заказ, который в
        1С есть, отчитывался как «не найден в выборке».

        Воспроизводится только заказом БЕЗ даты в представлении: с датой он
        находится точечно, и перебор не задействуется.
        """
        february = ORDER_ROWS[0].model_copy(
            update={"number": "ТД000000198", "document_id": "uuid-feb", "date": datetime(2026, 2, 10)}
        )
        recent = ORDER_ROWS[0].model_copy(
            update={"number": "ТД000000900", "document_id": "uuid-recent",
                    "date": datetime.combine(date.today() - timedelta(days=3), datetime.min.time())}
        )
        client = FakeOneSClient(order_rows=[february, recent])
        repository = _repository(client, one_s_bulk_max_chunks=4, one_s_bulk_chunk_days=30)
        base = _repository().list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))[0]

        self._lookup_by(repository, base, "ТД000000198", date(2026, 2, 10))
        # Даты нет — остаётся только слепой перебор, он должен идти от сегодня.
        without_hint = self._lookup_by(repository, base, "ТД000000900", None)

        self.assertEqual(without_hint.status, "found")

    def test_walk_result_does_not_depend_on_document_order(self):
        """Тот же набор в обратном порядке обязан дать тот же результат."""
        orders = [
            ORDER_ROWS[0].model_copy(
                update={"number": "ТД000000198", "document_id": "uuid-feb",
                        "date": datetime(2026, 2, 10)}
            ),
            ORDER_ROWS[0].model_copy(
                update={"number": "ТД000000900", "document_id": "uuid-recent",
                        "date": datetime.combine(date.today() - timedelta(days=3), datetime.min.time())}
            ),
        ]
        pairs = [("ТД000000198", date(2026, 2, 10)), ("ТД000000900", None)]
        base = _repository().list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))[0]

        statuses = []
        for order_of_documents in (pairs, list(reversed(pairs))):
            repository = _repository(
                FakeOneSClient(order_rows=orders), one_s_bulk_max_chunks=4, one_s_bulk_chunk_days=30
            )
            statuses.append(
                {number: self._lookup_by(repository, base, number, day).status
                 for number, day in order_of_documents}
            )

        self.assertEqual(statuses[0], statuses[1])
        self.assertEqual({"ТД000000198": "found", "ТД000000900": "found"}, statuses[0])

    def test_targeted_lookup_does_not_mark_index_exhausted(self):
        client = FakeOneSClient()
        repository = _repository(client)

        repository.find_supplier_order(self._expense())

        self.assertFalse(repository._orders.exhausted)
        self.assertEqual(0, repository._orders.walk_requests)

    def test_index_is_reused_between_lookups(self):
        client = FakeOneSClient()
        repository = _repository(client)

        repository.find_supplier_order(self._expense())
        repository.find_supplier_order(self._expense())

        self.assertEqual(1, len(client.order_queries))


class ReloadTests(NetworkGuardTestCase):
    def _first_expense(self, repository):
        items = repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        return next(item for item in items if item.number == "ТД000000840")

    def test_reload_finds_document_by_identifier(self):
        repository = _repository()
        expense = self._first_expense(repository)

        reloaded = repository.reload_additional_expense(expense)

        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.ref, expense.ref)

    def test_reload_requests_day_of_document_with_slack(self):
        client = FakeOneSClient()
        repository = _repository(client)
        expense = self._first_expense(repository)

        repository.reload_additional_expense(expense)

        query = client.expense_queries[-1]
        self.assertEqual(query.period_begin, date(2026, 8, 10) - timedelta(days=1))
        self.assertEqual(query.period_end, date(2026, 8, 10) + timedelta(days=1))

    def test_missing_document_returns_none(self):
        repository = _repository()
        expense = self._first_expense(repository)
        repository._client.expense_rows = []

        self.assertIsNone(repository.reload_additional_expense(expense))


class WriteTests(NetworkGuardTestCase):
    def _expense(self, repository):
        items = repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        return next(item for item in items if item.number == "ТД000000840")

    def test_success_is_confirmed_by_rereading(self):
        written = _expense_row_with(
            incoming_number="4726385", incoming_date=date(2026, 8, 17)
        )
        client = _WritingClient(after_write=written)
        repository = _repository(client)
        expense = self._expense(repository)

        outcome = repository.write_incoming_requisites(expense, "4726385", date(2026, 8, 17))

        self.assertEqual(outcome.status, "written")
        self.assertEqual(1, client.write_calls)

    def test_unchanged_requisites_are_not_reported_as_success(self):
        """Ответ 1С мог быть каким угодно — решает состояние документа."""
        client = _WritingClient()
        repository = _repository(client)
        expense = self._expense(repository)

        outcome = repository.write_incoming_requisites(expense, "4726385", date(2026, 8, 17))

        self.assertEqual(outcome.status, "unknown")
        self.assertIn("не изменились", outcome.message)

    def test_foreign_requisites_after_write_are_conflict(self):
        foreign = _expense_row_with(incoming_number="OTHER-77", incoming_date=date(2026, 8, 1))
        client = _WritingClient(after_write=foreign)
        repository = _repository(client)
        expense = self._expense(repository)

        outcome = repository.write_incoming_requisites(expense, "4726385", date(2026, 8, 17))

        self.assertEqual(outcome.status, "conflict")

    def test_explicit_error_skips_rereading(self):
        client = _WritingClient(response={"code": 500, "error": "Документ проведён"})
        repository = _repository(client)
        expense = self._expense(repository)
        queries_before = len(client.expense_queries)

        outcome = repository.write_incoming_requisites(expense, "4726385", date(2026, 8, 17))

        self.assertEqual(outcome.status, "unknown")
        self.assertIn("Документ проведён", outcome.message)
        self.assertEqual(queries_before, len(client.expense_queries))

    def test_write_request_body(self):
        captured = {}

        class _CapturingClient(_WritingClient):
            def update_incoming_requisites(self, request):
                captured.update(request.to_payload())
                return super().update_incoming_requisites(request)

        client = _CapturingClient()
        repository = _repository(client)
        expense = self._expense(repository)

        repository.write_incoming_requisites(expense, "4726/385", date(2026, 8, 16))

        self.assertEqual(captured["docID"], expense.ref)
        # Номер уходит без нормализации: слеш в нём значим.
        self.assertEqual(captured["incomingdocnumber"], "4726/385")
        self.assertEqual(captured["incomingdocdate"], "20260816")


class ResponseParsingTests(NetworkGuardTestCase):
    def test_empty_body_is_not_an_error(self):
        result = UpdateIncomingRequisitesResult.from_response({})

        self.assertFalse(result.explicit_error)

    def test_non_json_body_is_not_an_error(self):
        result = UpdateIncomingRequisitesResult.from_response("что угодно")

        self.assertFalse(result.explicit_error)
        self.assertEqual(result.raw, "что угодно")

    def test_russian_error_key_is_recognized(self):
        result = UpdateIncomingRequisitesResult.from_response({"Ошибка": "Нет прав"})

        self.assertTrue(result.explicit_error)
        self.assertEqual(result.message, "Нет прав")

    def test_false_status_is_recognized(self):
        result = UpdateIncomingRequisitesResult.from_response({"success": False})

        self.assertTrue(result.explicit_error)


class WriteGuardTests(NetworkGuardTestCase):
    def test_guard_error_is_not_swallowed(self):
        from app.clients.one_s import OneSClient

        repository = OneSRepository(
            test_settings(one_s_writes_enabled=False),
            client=OneSClient(test_settings(one_s_writes_enabled=False)),
        )
        expense = _repository().list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))[0]

        with self.assertRaises(RuntimeError):
            repository.write_incoming_requisites(expense, "4726385", date(2026, 8, 17))


class SupplierOrderAttributeTests(NetworkGuardTestCase):
    """Табличная часть «Даты» приходит отдельным методом по идентификатору заказа."""

    def _expense(self, repository):
        items = repository.list_additional_expenses(date(2026, 8, 1), date(2026, 8, 31))
        return next(i for i in items if i.supplier_order.number)

    def test_dates_are_attached_to_found_order(self):
        client = FakeOneSClient()
        repository = _repository(client)

        lookup = repository.find_supplier_order(self._expense(repository))

        self.assertEqual(lookup.status, "found")
        names = {row.name for row in lookup.order.date_rows}
        self.assertIn("Дата автовывоза со станции/порта = Пост.", names)

    def test_auto_pickup_fact_is_available(self):
        """Ради этой даты всё и делалось: без неё запись невозможна."""
        repository = _repository(FakeOneSClient())

        lookup = repository.find_supplier_order(self._expense(repository))
        row = next(r for r in lookup.order.date_rows if "автовывоза" in r.name)

        self.assertEqual(row.fact, date(2026, 8, 21))
        self.assertEqual(row.plan, date(2026, 7, 20))
        self.assertEqual(row.normative, date(2026, 7, 30))

    def test_attributes_are_requested_by_order_identifier(self):
        client = FakeOneSClient()
        repository = _repository(client)

        lookup = repository.find_supplier_order(self._expense(repository))

        self.assertEqual(client.attribute_calls, [lookup.order.ref])

    def test_attributes_are_cached_per_order(self):
        """Один заказ обычно делят несколько доп. расходов."""
        client = FakeOneSClient()
        repository = _repository(client)
        expense = self._expense(repository)

        repository.find_supplier_order(expense)
        repository.find_supplier_order(expense)

        self.assertEqual(1, len(client.attribute_calls))

    def test_failure_to_load_attributes_does_not_break_lookup(self):
        """Без дат документ уйдёт в ручную обработку, но не в техническую ошибку."""
        client = FakeOneSClient()
        repository = _repository(client)
        expense = self._expense(repository)

        def _fail(document_id):
            raise HttpClientError("1С недоступна")

        client.list_supplier_order_attributes = _fail
        lookup = repository.find_supplier_order(expense)

        self.assertEqual(lookup.status, "found")
        self.assertEqual(lookup.order.date_rows, [])

    def test_non_date_attributes_are_ignored(self):
        rows = [
            SupplierOrderAttributeRow.model_validate({"Свойство": "Судно", "Значение": "VESSEL ONE"}),
            SupplierOrderAttributeRow.model_validate({"Свойство": "Порт выхода", "Значение": "Порт А"}),
        ]
        client = FakeOneSClient(attribute_rows=rows)
        repository = _repository(client)

        lookup = repository.find_supplier_order(self._expense(repository))

        self.assertEqual(lookup.order.date_rows, [])


if __name__ == "__main__":
    unittest.main()
