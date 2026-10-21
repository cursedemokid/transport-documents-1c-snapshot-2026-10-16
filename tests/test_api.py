"""Проверка HTTP-слоя приложения на реальном ASGI-стеке."""

import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.services.sync_service import SyncService

from tests.support.fakes import FakeOneSRepository, RecordingReportService, SabyStub, saby_doc
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import test_settings


class ApiTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        import main

        self.main = main

    def _client(self, **settings_overrides) -> TestClient:
        settings = test_settings(**settings_overrides)
        # lifespan поднимает планировщик из настроек окружения — подменяем их,
        # чтобы тест не зависел от машины разработчика.
        patcher = mock.patch("app.config.Settings.from_env", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        return TestClient(self.main.app)

    def test_root_and_health(self):
        with self._client() as client:
            self.assertEqual(client.get("/").json()["status"], "ok")
            self.assertEqual(client.get("/health").json(), {"status": "ok"})

    def test_sync_run_returns_counts_and_effective_mode(self):
        repository = FakeOneSRepository()
        service = mock.patch(
            "app.api.routes.SyncService",
            side_effect=lambda settings: SyncService(
                settings,
                saby_client=SabyStub([saby_doc()]),
                repository=repository,
                report_service=RecordingReportService(),
            ),
        )
        with service, self._client() as client:
            response = client.post(
                "/sync/run",
                json={
                    "date_from": "2026-08-17",
                    "date_to": "2026-08-17",
                    "dry_run": True,
                    "send_report": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counts"], {"dry_run_ready": 1})
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["writes_enabled"])
        self.assertEqual(0, repository.write_calls)

    def test_sync_run_never_writes_even_with_dry_run_false(self):
        """Запрос не может пересилить выключенную настройку записи."""
        repository = FakeOneSRepository()
        service = mock.patch(
            "app.api.routes.SyncService",
            side_effect=lambda settings: SyncService(
                settings,
                saby_client=SabyStub([saby_doc()]),
                repository=repository,
                report_service=RecordingReportService(),
            ),
        )
        with service, self._client(one_s_writes_enabled=False) as client:
            response = client.post(
                "/sync/run",
                json={
                    "date_from": "2026-08-17",
                    "date_to": "2026-08-17",
                    "dry_run": False,
                    "send_report": False,
                },
            )

        payload = response.json()
        self.assertEqual(payload["counts"], {"dry_run_ready": 1})
        self.assertEqual(0, repository.write_calls)


#: Ключ ездит в HTTP-заголовке — только ASCII.
API_KEY = "demo-credential"


class SyncRunGuardTests(ApiTests):
    """Охрана запуска: ключ и запрет параллельных прогонов."""

    def _stub_service(self, run=None):
        def _factory(settings):
            service = mock.Mock()
            service.run = run or (lambda request: _empty_response())
            return service

        return mock.patch("app.api.routes.SyncService", side_effect=_factory)

    def test_key_is_required_when_configured(self):
        with self._stub_service(), self._client(sync_api_key=API_KEY) as client:
            response = client.post("/sync/run", json={"send_report": False})

        self.assertEqual(401, response.status_code)

    def test_wrong_key_is_rejected(self):
        with self._stub_service(), self._client(sync_api_key=API_KEY) as client:
            response = client.post(
                "/sync/run", json={"send_report": False}, headers={"X-API-Key": "wrong-demo-credential"}
            )

        self.assertEqual(401, response.status_code)

    def test_correct_key_is_accepted(self):
        with self._stub_service(), self._client(sync_api_key=API_KEY) as client:
            response = client.post(
                "/sync/run", json={"send_report": False}, headers={"X-API-Key": API_KEY}
            )

        self.assertEqual(200, response.status_code)

    def test_writes_without_a_key_are_refused(self):
        """Второй рубеж. Первый — Settings.validate(), он не даёт такому сервису стартовать.

        Но если сервис подняли в обход валидации, неаутентифицированный
        `{"dry_run": false}` писал бы в продуктив. Поэтому отказ есть и здесь.
        """
        from fastapi import HTTPException

        from app.api.routes import _require_api_key

        settings = test_settings(one_s_writes_enabled=True, sync_api_key=None)

        with self.assertRaises(HTTPException) as caught:
            _require_api_key(settings, None)

        self.assertEqual(503, caught.exception.status_code)
        self.assertIn("SYNC_API_KEY", caught.exception.detail)

    def test_no_key_configured_and_no_writes_is_allowed(self):
        """Наблюдательный стенд без записи ключом закрывать не обязательно."""
        from app.api.routes import _require_api_key

        _require_api_key(test_settings(one_s_writes_enabled=False, sync_api_key=None), None)

    def test_second_run_gets_409_instead_of_waiting(self):
        """Прогон меняет общее состояние: два сразу недопустимы.

        Отказ должен быть немедленным и внятным — молчаливое ожидание конца
        первого прогона выглядит как зависший сервис.
        """
        started = threading.Event()
        release = threading.Event()

        def _slow_run(request):
            started.set()
            release.wait(timeout=5)
            return _empty_response()

        with self._stub_service(run=_slow_run), self._client() as client:
            first: list = []
            worker = threading.Thread(
                target=lambda: first.append(client.post("/sync/run", json={"send_report": False}))
            )
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                second = client.post("/sync/run", json={"send_report": False})
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertEqual(409, second.status_code)
        self.assertEqual(200, first[0].status_code)

    def test_lock_is_released_after_a_failed_run(self):
        calls = {"n": 0}

        def _failing_then_ok(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("прогон сорвался")
            return _empty_response()

        with self._stub_service(run=_failing_then_ok), self._client() as client:
            with self.assertRaises(RuntimeError):
                client.post("/sync/run", json={"send_report": False})
            response = client.post("/sync/run", json={"send_report": False})

        self.assertEqual(200, response.status_code)


def _empty_response():
    from datetime import date

    from app.schemas.sync import SyncRunResponse

    return SyncRunResponse(
        request_id="test",
        dry_run=True,
        date_from=date(2026, 8, 17),
        date_to=date(2026, 8, 17),
        total_saby_documents=0,
    )


if __name__ == "__main__":
    unittest.main()
