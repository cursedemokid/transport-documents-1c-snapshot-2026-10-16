import threading
import unittest
from unittest.mock import patch

from app.schemas.sync import SyncRunRequest
from app.services.run_guard import RUN_LOCK, SyncAlreadyRunningError
from app.services.scheduler import DailySyncScheduler
from app.services.sync_service import SyncService
from tests.support.settings import test_settings


class ExecutionGuardTests(unittest.TestCase):
    def test_scheduler_cannot_start_while_a_manual_run_is_active(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_manual_run():
            with RUN_LOCK:
                entered.set()
                release.wait(5)

        worker = threading.Thread(target=hold_manual_run)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with patch.object(SyncService, '_run') as run:
                with self.assertRaises(SyncAlreadyRunningError):
                    DailySyncScheduler(test_settings())._run_once()
                run.assert_not_called()
        finally:
            release.set()
            worker.join(5)

    def test_api_guard_can_enter_the_service_on_the_same_worker(self):
        service = SyncService(test_settings())
        with RUN_LOCK, patch.object(service, '_run', return_value='completed'):
            self.assertEqual(service.run(SyncRunRequest()), 'completed')
