import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.clients.http_json import JsonHttpClient
from app.config import Settings
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import default_settings


class SettingsDefaultsTests(NetworkGuardTestCase):
    def test_defaults_forbid_writes_and_scheduler(self):
        """Отсутствие настройки не должно означать разрешение записи в 1С."""
        settings = default_settings()

        self.assertFalse(settings.one_s_writes_enabled)
        self.assertFalse(settings.daily_sync_enabled)
        self.assertTrue(settings.daily_sync_dry_run)

    def test_from_env_defaults_forbid_writes_without_env_files(self):
        """То же самое для настоящего from_env() в каталоге без .env/develop.env."""
        with TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                with mock.patch.dict(os.environ, {}, clear=True):
                    settings = Settings.from_env()
            finally:
                os.chdir(previous)

        self.assertFalse(settings.one_s_writes_enabled)
        self.assertFalse(settings.daily_sync_enabled)
        self.assertTrue(settings.daily_sync_dry_run)

    def test_default_settings_do_not_read_local_env_files(self):
        """develop.env разработчика не должен протекать в тесты."""
        with mock.patch("app.config.load_env_files") as loader:
            default_settings()

        loader.assert_not_called()


class OneSSettingsTests(NetworkGuardTestCase):
    def test_warehouse_group_and_expense_type_defaults(self):
        """Группы «Москва» в 1С нет — по умолчанию разрешена группа ОСНОВНЫЕ."""
        settings = default_settings()

        self.assertEqual(settings.one_s_allowed_warehouse_groups, ["ОСНОВНЫЕ"])
        self.assertEqual(settings.one_s_allowed_expense_types, ["Фрахт", "ЖД РФ"])
        self.assertEqual(
            settings.one_s_auto_pickup_row_name, "Дата автовывоза со станции/порта = Пост."
        )

    def test_lists_are_read_from_environment(self):
        env = {
            "ONE_S_ALLOWED_WAREHOUSE_GROUPS": "ОСНОВНЫЕ, СЕВЕРНЫЙ",
            "ONE_S_ALLOWED_EXPENSE_TYPES": "Фрахт",
        }
        with mock.patch("app.config.load_env_files", return_value=None), mock.patch.dict(
            os.environ, env, clear=True
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.one_s_allowed_warehouse_groups, ["ОСНОВНЫЕ", "СЕВЕРНЫЙ"])
        self.assertEqual(settings.one_s_allowed_expense_types, ["Фрахт"])

    def test_matching_and_bulk_defaults(self):
        settings = default_settings()

        # 40, а не 60: при пороге 60 ни одна из 46 эталонных пар не находится —
        # оба сильных сигнала в реальных данных недоступны. Замер в .env.example.
        self.assertEqual(settings.match_min_score, 40)
        self.assertEqual(settings.match_score_margin, 20)
        self.assertEqual(settings.match_name_similarity, 0.85)
        self.assertEqual(settings.match_date_window_days, 45)
        self.assertEqual(settings.one_s_bulk_chunk_days, 92)
        self.assertEqual(settings.one_s_order_date_slack_days, 1)
        self.assertTrue(settings.allow_completing_partial_requisites)


class NetworkGuardTests(NetworkGuardTestCase):
    def test_guard_blocks_http_requests(self):
        client = JsonHttpClient("https://internal.internal.example.invalid/endpoint", timeout_seconds=1)

        with self.assertRaises(AssertionError):
            client.get_json("/whatever", {})


if __name__ == "__main__":
    unittest.main()
