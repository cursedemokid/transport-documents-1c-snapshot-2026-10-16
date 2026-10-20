"""Состояние между запусками и поведение отчёта, не зависящее от отправки."""

import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.schemas.sync import SyncItemResult, SyncRunRequest, SyncRunResponse
from app.services.report_labels import REPORT_GROUP_ORDER, status_group
from app.services.reporting import ReportService
from app.services.sync_service import SyncService
from app.services.sync_state import SyncStateStore
from tests.support.fakes import (
    FakeOneSRepository,
    InMemoryStateStore,
    RecordingReportService,
    SabyStub,
    saby_doc,
    temporary_http_error,
)
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import test_settings


class StateStoreTests(NetworkGuardTestCase):
    def test_roundtrip(self):
        with TemporaryDirectory() as directory:
            store = SyncStateStore(Path(directory) / "state.json")
            store.save_last_processed_date(date(2026, 8, 20))

            self.assertEqual(store.load_last_processed_date(), date(2026, 8, 20))

    def test_missing_file_is_none(self):
        with TemporaryDirectory() as directory:
            store = SyncStateStore(Path(directory) / "nope.json")

            self.assertIsNone(store.load_last_processed_date())

    def test_broken_file_does_not_raise(self):
        """Состояние — подсказка, а не источник истины."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("не json", encoding="utf-8")

            self.assertIsNone(SyncStateStore(path).load_last_processed_date())

    def test_state_never_moves_backwards(self):
        with TemporaryDirectory() as directory:
            store = SyncStateStore(Path(directory) / "state.json")
            store.save_last_processed_date(date(2026, 8, 20))
            store.save_last_processed_date(date(2026, 8, 1))

            self.assertEqual(store.load_last_processed_date(), date(2026, 8, 20))

    def test_creates_parent_directory(self):
        with TemporaryDirectory() as directory:
            store = SyncStateStore(Path(directory) / "nested" / "state.json")
            store.save_last_processed_date(date(2026, 8, 20))

            saved = json.loads((Path(directory) / "nested" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["last_processed_date"], "2026-08-20")


class WindowExtensionTests(NetworkGuardTestCase):
    def _service(self, state):
        return SyncService(
            test_settings(),
            saby_client=SabyStub([]),
            repository=FakeOneSRepository(),
            state_store=state,
        )

    def test_gap_since_last_run_extends_window_back(self):
        """Пропуск запусков не должен оставлять дыру в выборке."""
        gap_start = date.today() - timedelta(days=60)
        service = self._service(InMemoryStateStore(last_processed_date=gap_start))

        date_from, date_to = service._resolve_period(SyncRunRequest())

        self.assertEqual(date_from, gap_start + timedelta(days=1))
        self.assertEqual(date_to, date.today())

    def test_recent_run_does_not_narrow_window(self):
        """Окно можно только расширять: сдвиг вперёд терял бы документы."""
        yesterday = date.today() - timedelta(days=1)
        service = self._service(InMemoryStateStore(last_processed_date=yesterday))

        date_from, _ = service._resolve_period(SyncRunRequest())

        self.assertEqual(date_from, date.today() - timedelta(days=14))

    def test_extension_is_bounded_by_lookback_limit(self):
        service = SyncService(
            test_settings(sync_max_lookback_days=30, sync_start_date=date(2020, 1, 1)),
            saby_client=SabyStub([]),
            repository=FakeOneSRepository(),
            state_store=InMemoryStateStore(last_processed_date=date(2021, 1, 1)),
        )

        date_from, _ = service._resolve_period(SyncRunRequest())

        self.assertEqual(date_from, date.today() - timedelta(days=30))

    def test_explicit_dates_are_never_overridden(self):
        service = self._service(InMemoryStateStore(last_processed_date=date(2020, 1, 1)))

        period = service._resolve_period(
            SyncRunRequest(date_from=date(2026, 8, 1), date_to=date(2026, 8, 20))
        )

        self.assertEqual(period, (date(2026, 8, 1), date(2026, 8, 20)))

    def test_period_is_remembered_after_run(self):
        state = InMemoryStateStore()
        service = SyncService(
            test_settings(),
            saby_client=SabyStub([saby_doc()]),
            repository=FakeOneSRepository(),
            state_store=state,
        )

        service.run(SyncRunRequest(date_from=date(2026, 8, 17), date_to=date(2026, 8, 17), send_report=False))

        self.assertEqual(state.last_processed_date, date(2026, 8, 17))

    def test_period_is_not_remembered_when_saby_fails(self):
        """Иначе неудачный период был бы пропущен навсегда."""
        state = InMemoryStateStore()
        service = SyncService(
            test_settings(),
            saby_client=SabyStub([], error=RuntimeError("Saby недоступен")),
            repository=FakeOneSRepository(),
            state_store=state,
        )

        service.run(SyncRunRequest(send_report=False))

        self.assertIsNone(state.last_processed_date)


class ReportAlwaysSavedTests(NetworkGuardTestCase):
    def test_file_is_written_even_without_sending(self):
        with TemporaryDirectory() as directory:
            settings = test_settings(report_dir=directory)
            service = SyncService(
                settings,
                saby_client=SabyStub([saby_doc()]),
                repository=FakeOneSRepository(),
                report_service=ReportService(settings),
                state_store=InMemoryStateStore(),
            )

            response = service.run(
                SyncRunRequest(
                    date_from=date(2026, 8, 17), date_to=date(2026, 8, 17), send_report=False
                )
            )

            files = list(Path(directory).glob("*"))

        self.assertFalse(response.report_sent)
        self.assertEqual(1, len(files), "отчёт должен сохраняться и без отправки письма")


class ReportGroupingTests(NetworkGuardTestCase):
    def test_dry_run_is_not_reported_as_written(self):
        """Пока запись выключена, в этом статусе оказываются все документы —
        называть их успешно записанными нельзя."""
        self.assertNotEqual(status_group("dry_run_ready"), status_group("written"))
        self.assertEqual(status_group("dry_run_ready"), "Проверено, запись не выполнялась")

    def test_every_group_is_listed_in_order(self):
        for status in ("written", "dry_run_ready", "already_filled", "manual", "conflict",
                       "not_found", "ambiguous", "business_error", "technical_error"):
            with self.subTest(status=status):
                self.assertIn(status_group(status), REPORT_GROUP_ORDER)

    def test_group_reaches_the_report_file(self):
        report = SyncRunResponse(
            request_id="r",
            dry_run=True,
            writes_enabled=False,
            date_from=date(2026, 8, 17),
            date_to=date(2026, 8, 17),
            total_saby_documents=1,
            results=[SyncItemResult(status="dry_run_ready", message="Проверки пройдены")],
        )

        with TemporaryDirectory() as directory:
            from app.services.report_file import save_report_file

            text = save_report_file(report, directory, "csv").read_text(encoding="utf-8-sig")

        self.assertIn("Проверено, запись не выполнялась", text)
        self.assertNotIn("Успешно записано", text)


class StateAdvancementTests(NetworkGuardTestCase):
    """Когда период можно считать пройденным.

    Окно умеет только расширяться назад, поэтому пропущенный период больше не
    вернётся: любая причина усомниться должна оставлять его непройденным.
    """

    def _service(self, state, repository=None, documents=None, **overrides):
        return SyncService(
            test_settings(**overrides),
            saby_client=SabyStub(documents if documents is not None else [saby_doc()]),
            repository=repository or FakeOneSRepository(),
            report_service=RecordingReportService(),
            state_store=state,
        )

    def _run(self, service, **overrides):
        params = {"date_from": date(2026, 8, 17), "date_to": date(2026, 8, 17), "send_report": False}
        params.update(overrides)
        return service.run(SyncRunRequest(**params))

    def test_clean_run_advances_state(self):
        state = InMemoryStateStore()

        self._run(self._service(state))

        self.assertEqual(state.last_processed_date, date(2026, 8, 17))

    def test_technical_error_holds_the_period(self):
        """Регрессия: период запоминался, даже когда все документы упали."""
        state = InMemoryStateStore()
        repository = FakeOneSRepository(expense_error=temporary_http_error())
        service = self._service(state, repository=repository, sync_max_attempts=1)

        response = self._run(service)

        self.assertEqual(["technical_error"], [item.status for item in response.results])
        self.assertIsNone(state.last_processed_date)

    def test_single_failure_among_successes_holds_the_period(self):
        state = InMemoryStateStore()
        documents = [saby_doc(incoming_number="4726385"), saby_doc(incoming_number="4726386")]
        repository = FakeOneSRepository(expense_error=temporary_http_error())
        service = self._service(state, repository=repository, documents=documents, sync_max_attempts=1)

        response = self._run(service)

        self.assertIn("technical_error", response.counts)
        self.assertIsNone(state.last_processed_date)

    def test_trimmed_selection_holds_the_period(self):
        """Регрессия: max_documents двигал состояние и выжигал необработанный хвост."""
        state = InMemoryStateStore()
        documents = [saby_doc(incoming_number=f"47263{i:02d}") for i in range(5)]

        self._run(self._service(state, documents=documents), max_documents=2)

        self.assertIsNone(state.last_processed_date)

    def test_trimming_that_changes_nothing_still_advances(self):
        state = InMemoryStateStore()

        self._run(self._service(state), max_documents=10)

        self.assertEqual(state.last_processed_date, date(2026, 8, 17))

    def test_advance_state_false_holds_the_period(self):
        state = InMemoryStateStore()

        self._run(self._service(state), advance_state=False)

        self.assertIsNone(state.last_processed_date)

    def test_future_period_is_not_remembered(self):
        state = InMemoryStateStore()
        future = date.today() + timedelta(days=10)

        self._run(self._service(state), date_from=future, date_to=future)

        self.assertIsNone(state.last_processed_date)


if __name__ == "__main__":
    unittest.main()
