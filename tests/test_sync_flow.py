"""Поток синхронизации целиком: регрессии на дефекты и гарантия отсутствия записи."""

import unittest
from datetime import date, timedelta

from app.schemas.sync import SyncRunRequest
from app.services.one_s_repository import OneSRepository, WriteOutcome
from app.services.sync_service import SyncService
from tests.support.fakes import (
    FakeOneSClient,
    FakeOneSRepository,
    InMemoryStateStore,
    RecordingReportService,
    SabyStub,
    expense,
    order,
    saby_doc,
    temporary_http_error,
)
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import test_settings

PERIOD = {"date_from": date(2026, 8, 17), "date_to": date(2026, 8, 17)}


def _service(repository=None, documents=None, reporting=None, state=None, **overrides):
    return SyncService(
        test_settings(**overrides),
        saby_client=SabyStub(documents if documents is not None else [saby_doc()]),
        repository=repository or FakeOneSRepository(),
        report_service=reporting or RecordingReportService(),
        state_store=state or InMemoryStateStore(),
    )


def _run(service, **request_overrides):
    params = {**PERIOD, "send_report": False}
    params.update(request_overrides)
    return service.run(SyncRunRequest(**params))


class PeriodTests(NetworkGuardTestCase):
    def _period(self, **kwargs):
        return _service()._resolve_period(SyncRunRequest(**kwargs))

    def test_only_date_to_gives_full_window(self):
        """Раньше окно схлопывалось в один день и документы терялись."""
        date_from, date_to = self._period(date_to=date(2026, 8, 20))

        self.assertEqual(date_to, date(2026, 8, 20))
        self.assertEqual(date_from, date(2026, 8, 20) - timedelta(days=14))

    def test_both_dates_are_kept(self):
        self.assertEqual(
            self._period(date_from=date(2026, 8, 1), date_to=date(2026, 8, 20)),
            (date(2026, 8, 1), date(2026, 8, 20)),
        )

    def test_reversed_interval_is_normalized(self):
        self.assertEqual(
            self._period(date_from=date(2026, 8, 20), date_to=date(2026, 8, 1)),
            (date(2026, 8, 1), date(2026, 8, 20)),
        )

    def test_future_date_from_does_not_invert_interval(self):
        date_from, date_to = self._period(date_from=date.today() + timedelta(days=30))

        self.assertLessEqual(date_from, date_to)

    def test_expense_period_is_wider_than_saby_window(self):
        """Доп. расход в 1С может быть проведён и раньше, и позже УПД."""
        service = _service()

        expenses_from, expenses_to = service._expense_period(date(2026, 8, 1), date(2026, 8, 20))

        self.assertEqual(expenses_from, date(2026, 8, 1) - timedelta(days=45))
        self.assertEqual(expenses_to, date(2026, 8, 20) + timedelta(days=14))

    def test_expense_period_never_precedes_sync_start_date(self):
        service = _service(sync_start_date=date(2026, 7, 20))

        expenses_from, _ = service._expense_period(date(2026, 8, 1), date(2026, 8, 20))

        self.assertEqual(expenses_from, date(2026, 7, 20))


class SabyFailureTests(NetworkGuardTestCase):
    def test_report_is_sent_and_flag_is_set(self):
        """Раньше в этой ветке report_sent оставался False, даже если письмо ушло."""
        reporting = RecordingReportService()
        service = SyncService(
            test_settings(),
            saby_client=SabyStub([], error=RuntimeError("Saby недоступен")),
            repository=FakeOneSRepository(),
            report_service=reporting,
            state_store=InMemoryStateStore(),
        )

        response = service.run(SyncRunRequest(send_report=True))

        self.assertEqual(["technical_error"], [item.status for item in response.results])
        self.assertTrue(response.report_sent)
        self.assertEqual(1, reporting.sent)

    def test_report_is_not_sent_when_not_requested(self):
        reporting = RecordingReportService()
        service = SyncService(
            test_settings(),
            saby_client=SabyStub([], error=RuntimeError("Saby недоступен")),
            repository=FakeOneSRepository(),
            report_service=reporting,
            state_store=InMemoryStateStore(),
        )

        response = service.run(SyncRunRequest(send_report=False))

        self.assertFalse(response.report_sent)
        self.assertEqual(0, reporting.sent)


class WriteGuardTests(NetworkGuardTestCase):
    def test_no_write_when_dry_run_requested(self):
        repository = FakeOneSRepository()

        response = _run(_service(repository), dry_run=True)

        self.assertEqual(["dry_run_ready"], [item.status for item in response.results])
        self.assertIn("dry_run", response.results[0].message)
        self.assertEqual(0, repository.write_calls)

    def test_no_write_even_when_dry_run_is_false(self):
        """Выключенная настройка сильнее запроса: до POST дело не доходит."""
        repository = FakeOneSRepository()

        response = _run(_service(repository, one_s_writes_enabled=False), dry_run=False)

        self.assertEqual(["dry_run_ready"], [item.status for item in response.results])
        self.assertIn("ONE_S_WRITES_ENABLED", response.results[0].message)
        self.assertEqual(0, repository.write_calls)

    def test_response_reports_effective_mode(self):
        response = _run(_service(one_s_writes_enabled=False), dry_run=False)

        self.assertTrue(response.dry_run)
        self.assertFalse(response.writes_enabled)

    def test_blocked_write_is_not_retried(self):
        repository = FakeOneSRepository()
        service = _service(repository, sync_max_attempts=3, one_s_writes_enabled=False)

        response = _run(service, dry_run=False)

        self.assertEqual([1], [item.attempt for item in response.results])
        self.assertEqual(1, repository.expense_calls)


class AutoWriteGateTests(NetworkGuardTestCase):
    """Программа не принимает решений сама.

    Даже когда кандидат единственный и все проверки пройдены, запись делает
    человек через форму подтверждения. Это главное требование этапа, и
    `ONE_S_WRITES_ENABLED=true` его не отменяет.
    """

    def test_run_does_not_write_without_auto_write(self):
        repository = FakeOneSRepository()  # запись провалит тест

        response = _run(_service(repository, one_s_writes_enabled=True), dry_run=False)

        self.assertEqual(["awaiting_confirmation"], [item.status for item in response.results])
        self.assertEqual(0, repository.write_calls)

    def test_awaiting_item_carries_its_candidate(self):
        """Форме нужен сам кандидат, а не фраза с объяснением."""
        response = _run(
            _service(FakeOneSRepository(), one_s_writes_enabled=True), dry_run=False
        )

        item = response.results[0]
        self.assertEqual(1, len(item.candidates))
        self.assertEqual(item.additional_expense_ref, item.candidates[0].ref)

    def test_ambiguous_item_carries_all_candidates(self):
        first = expense(ref="uuid-1", number="ТД000000001")
        second = expense(ref="uuid-2", number="ТД000000002")
        repository = FakeOneSRepository(expenses=[first, second])

        response = _run(_service(repository), dry_run=True)

        item = response.results[0]
        self.assertEqual("ambiguous", item.status)
        self.assertEqual({"uuid-1", "uuid-2"}, {c.ref for c in item.candidates})

    def test_auto_write_still_possible_when_explicitly_enabled(self):
        """Настройка заложена для будущего: выключена, но работает."""
        repository = FakeOneSRepository(allow_writes=True)

        response = _run(
            _service(repository, one_s_writes_enabled=True, one_s_auto_write_enabled=True),
            dry_run=False,
        )

        self.assertEqual(["written"], [item.status for item in response.results])


class WriteFlowTests(NetworkGuardTestCase):
    def _repository(self, **overrides):
        params = {"allow_writes": True}
        params.update(overrides)
        return FakeOneSRepository(**params)

    def test_document_is_reread_before_write(self):
        repository = self._repository()

        response = _run(
            _service(repository, one_s_writes_enabled=True, one_s_auto_write_enabled=True), dry_run=False
        )

        self.assertEqual(["written"], [item.status for item in response.results])
        self.assertGreaterEqual(repository.reload_calls, 1)

    def test_conflict_from_repository_is_reported(self):
        repository = self._repository(
            write_outcome=WriteOutcome(status="conflict", message="После записи другие реквизиты")
        )

        response = _run(_service(repository, one_s_writes_enabled=True, one_s_auto_write_enabled=True), dry_run=False)

        self.assertEqual(["conflict"], [item.status for item in response.results])

    def test_unknown_result_is_technical_error_and_not_retried(self):
        """Повторять запись вслепую нельзя."""
        repository = self._repository(
            write_outcome=WriteOutcome(status="unknown", message="Реквизиты не изменились")
        )
        service = _service(repository, sync_max_attempts=3, one_s_writes_enabled=True, one_s_auto_write_enabled=True)

        response = _run(service, dry_run=False)

        self.assertEqual(["technical_error"], [item.status for item in response.results])
        self.assertEqual([1], [item.attempt for item in response.results])
        self.assertEqual(1, repository.write_calls)


class RetryTests(NetworkGuardTestCase):
    def test_network_failure_is_retried_and_replaces_result(self):
        repository = FakeOneSRepository(expense_error=temporary_http_error())
        service = _service(repository, sync_max_attempts=2)

        response = _run(service)

        # Один документ Saby — ровно одна строка отчёта, с номером последней попытки.
        self.assertEqual(["dry_run_ready"], [item.status for item in response.results])
        self.assertEqual([2], [item.attempt for item in response.results])
        self.assertEqual({"dry_run_ready": 1}, response.counts)

    def test_business_outcome_is_not_retried(self):
        repository = FakeOneSRepository(orders=[])
        service = _service(repository, sync_max_attempts=3)

        response = _run(service)

        self.assertEqual(["business_error"], [item.status for item in response.results])
        self.assertEqual([1], [item.attempt for item in response.results])

    def test_not_found_is_not_retried(self):
        repository = FakeOneSRepository(expenses=[])
        service = _service(repository, sync_max_attempts=3)

        response = _run(service)

        result = response.results[0]
        self.assertEqual("not_found", result.status)
        self.assertFalse(result.retryable)
        self.assertEqual(1, result.attempt)


class RequestBudgetTests(NetworkGuardTestCase):
    def test_expenses_are_fetched_once_for_all_documents(self):
        """Регрессия на прежний перебор окон: до 13 запросов 1С на каждый документ.

        Считаются обращения к обмену, а не к репозиторию: мемоизация живёт
        именно в нём, поэтому проверять надо сквозь настоящий репозиторий.
        """
        documents = [saby_doc(incoming_number=f"47263{index:02d}") for index in range(20)]
        client = FakeOneSClient()
        settings = test_settings()
        service = SyncService(
            settings,
            saby_client=SabyStub(documents),
            repository=OneSRepository(settings, client=client),
            report_service=RecordingReportService(),
            state_store=InMemoryStateStore(),
        )

        _run(service)

        self.assertEqual(1, len(client.expense_queries))
        self.assertLessEqual(len(client.order_queries), 3)

    def test_max_documents_trims_but_reports_full_size(self):
        """Регрессия: обрезанное число выдавалось за полный размер выборки,
        и пропущенный хвост был невидим."""
        documents = [saby_doc(incoming_number=f"47263{index:02d}") for index in range(5)]
        repository = FakeOneSRepository()

        response = _run(_service(repository, documents=documents), max_documents=2)

        self.assertEqual(5, response.total_saby_documents)
        self.assertEqual(2, response.processed_documents)
        self.assertEqual(2, len(response.results))


class OrderResolutionTests(NetworkGuardTestCase):
    def test_missing_order_number_is_manual(self):
        repository = FakeOneSRepository(expenses=[expense(order_number=None)])

        response = _run(_service(repository, documents=[saby_doc(order_number=None)]))

        self.assertEqual(["manual"], [item.status for item in response.results])

    def test_ambiguous_order_is_manual(self):
        repository = FakeOneSRepository(orders=[order(), order()])

        response = _run(_service(repository))

        self.assertEqual(["manual"], [item.status for item in response.results])


if __name__ == "__main__":
    unittest.main()
