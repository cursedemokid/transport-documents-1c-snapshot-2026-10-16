"""Разбор переменных окружения на РЕАЛЬНЫХ значениях.

Все остальные тесты зовут `Settings.from_env()` с вычищенным окружением, поэтому
каждый `os.getenv` уходит в значение по умолчанию, и собственно разбор не
проверяется. Здесь переменные подставляются явно.
"""

import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.config import ConfigError, Settings, _normalize_report_backend
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import default_settings


def _settings_from(env: dict[str, str]) -> Settings:
    with (
        mock.patch("app.config.load_env_files", return_value=None),
        mock.patch.dict(os.environ, env, clear=True),
    ):
        return Settings.from_env()


class ReportBackendTests(NetworkGuardTestCase):
    def test_combined_values_resolve_to_both(self):
        """Регрессия: «smtp+console» резолвился в «console», и письмо не уходило."""
        for value in ("smtp+console", "console+smtp", "both", "all"):
            with self.subTest(value=value):
                self.assertEqual(_normalize_report_backend(value), "both")

    def test_plain_values(self):
        cases = {
            "console": "console",
            "  CONSOLE  ": "console",
            "smtp": "smtp",
            "email": "smtp",
            "none": "none",
            "off": "none",
            "disabled": "none",
            "dummy": "none",
            "locmem": "none",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(_normalize_report_backend(value), expected)

    def test_django_style_names(self):
        self.assertEqual(
            _normalize_report_backend("django.core.mail.backends.console.EmailBackend"),
            "console",
        )
        self.assertEqual(
            _normalize_report_backend("django.core.mail.backends.smtp.EmailBackend"),
            "smtp",
        )

    def test_unknown_value_is_rejected(self):
        """Раньше опечатка возвращалась как есть и молча отключала доставку."""
        with self.assertRaises(ConfigError) as caught:
            _normalize_report_backend("mailgun")

        self.assertIn("REPORT_BACKEND", str(caught.exception))

    def test_email_backend_is_used_as_fallback(self):
        settings = _settings_from({"EMAIL_BACKEND": "console"})

        self.assertEqual(settings.report_backend, "console")


class NumericAndDateTests(NetworkGuardTestCase):
    def test_empty_value_falls_back_to_default(self):
        """Регрессия: SYNC_START_DATE= роняло старт необработанным ValueError."""
        settings = _settings_from(
            {"SYNC_START_DATE": "", "SABY_PAGE_SIZE": "", "AMOUNT_TOLERANCE": ""}
        )

        self.assertEqual(settings.sync_start_date, date(2026, 1, 1))
        self.assertEqual(settings.saby_page_size, 200)
        self.assertEqual(settings.amount_tolerance, 0.01)

    def test_valid_values_are_parsed(self):
        settings = _settings_from(
            {"SYNC_START_DATE": "2025-07-01", "SABY_PAGE_SIZE": " 50 ", "AMOUNT_TOLERANCE": "0.5"}
        )

        self.assertEqual(settings.sync_start_date, date(2025, 7, 1))
        self.assertEqual(settings.saby_page_size, 50)
        self.assertEqual(settings.amount_tolerance, 0.5)

    def test_broken_values_name_the_variable(self):
        cases = {
            "SABY_PAGE_SIZE": "abc",
            "AMOUNT_TOLERANCE": "0,01",
            "SYNC_START_DATE": "01.01.2026",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ConfigError) as caught:
                    _settings_from({name: value})
                self.assertIn(name, str(caught.exception))


class BooleanTests(NetworkGuardTestCase):
    def test_surrounding_spaces_are_ignored(self):
        """Регрессия: «True » с хвостовым пробелом молча выключал TLS."""
        self.assertTrue(_settings_from({"SMTP_USE_TLS": "True "}).smtp_use_tls)
        self.assertFalse(_settings_from({"SMTP_USE_TLS": " no "}).smtp_use_tls)

    def test_accepted_spellings(self):
        for value in ("1", "true", "YES", "on", "да"):
            with self.subTest(value=value):
                self.assertTrue(_settings_from({"ONE_S_WRITES_ENABLED": value}).one_s_writes_enabled)
        for value in ("0", "false", "NO", "off", "нет"):
            with self.subTest(value=value):
                self.assertFalse(
                    _settings_from({"ONE_S_WRITES_ENABLED": value}).one_s_writes_enabled
                )

    def test_unrecognised_value_is_rejected(self):
        """Молчаливый False на «tru» был бы безопасен для записи, но не для TLS."""
        with self.assertRaises(ConfigError) as caught:
            _settings_from({"ONE_S_WRITES_ENABLED": "tru"})

        self.assertIn("ONE_S_WRITES_ENABLED", str(caught.exception))


class CsvTests(NetworkGuardTestCase):
    def test_lists_are_split_and_trimmed(self):
        settings = _settings_from(
            {
                "ONE_S_ALLOWED_WAREHOUSE_GROUPS": "ОСНОВНЫЕ; СЕВЕРНЫЙ ,",
                "ERROR_EMAIL_TO": "developer@example.com, developer@example.com",
            }
        )

        self.assertEqual(settings.one_s_allowed_warehouse_groups, ["ОСНОВНЫЕ", "СЕВЕРНЫЙ"])
        self.assertEqual(settings.smtp_to, ["developer@example.com", "developer@example.com"])


class ValidateTests(NetworkGuardTestCase):
    def _valid(self, **overrides) -> Settings:
        params = {
            "report_backend": "none",
            "saby_login": "логин",
            "saby_REDACTED": "пароль",
            "one_s_base_url": "https://internal.internal.internal.example.invalid/hs/main",
            "one_s_username": "обмен",
            "one_s_REDACTED": "пароль",
        }
        params.update(overrides)
        return default_settings(**params)

    def test_valid_settings_pass(self):
        self._valid().validate()

    def test_smtp_backend_without_host_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self._valid(report_backend="smtp", smtp_host=None, smtp_to=["a@b.c"]).validate()

        self.assertIn("SMTP_HOST", str(caught.exception))

    def test_smtp_backend_without_recipients_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self._valid(report_backend="smtp", smtp_host="mail", smtp_to=[]).validate()

        self.assertIn("получатели", str(caught.exception))

    def test_writes_without_an_api_key_are_rejected(self):
        """С включённой записью открытый POST /sync/run меняет продуктивные данные."""
        with self.assertRaises(ConfigError) as caught:
            self._valid(
                one_s_writes_enabled=True, one_s_base_url="https://internal.internal.example.invalid/hs/main", sync_api_key=None
            ).validate()

        self.assertIn("SYNC_API_KEY", str(caught.exception))

    def test_writes_with_an_api_key_pass(self):
        self._valid(
            one_s_writes_enabled=True,
            one_s_base_url="https://internal.internal.example.invalid/hs/main",
            sync_api_key="REDACTED",
        ).validate()

    def test_missing_1c_connection_is_rejected_even_without_writes(self):
        """Читать из 1С нужно всегда.

        Без адреса сервис исправно стартовал и молча превращал каждый документ
        в техническую ошибку «Base URL is not configured».
        """
        with self.assertRaises(ConfigError) as caught:
            self._valid(one_s_base_url="").validate()

        self.assertIn("ONE_S_BASE_URL", str(caught.exception))

    def test_missing_1c_credentials_are_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self._valid(one_s_username=None, one_s_REDACTED=None).validate()

        self.assertIn("ONE_S_USERNAME", str(caught.exception))

    def test_writes_without_base_url_are_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self._valid(one_s_writes_enabled=True, one_s_base_url="").validate()

        self.assertIn("ONE_S_BASE_URL", str(caught.exception))

    def test_missing_saby_credentials_are_rejected(self):
        with self.assertRaises(ConfigError):
            self._valid(saby_login=None).validate()

    def test_future_start_date_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self._valid(sync_start_date=date.today() + timedelta(days=1)).validate()

        self.assertIn("SYNC_START_DATE", str(caught.exception))

    def test_attachment_limit_below_one_is_rejected(self):
        """При таком значении вложение молча выбрасывалось из каждого письма."""
        with self.assertRaises(ConfigError):
            self._valid(smtp_max_attachment_mb=0).validate()

    def test_all_problems_are_reported_at_once(self):
        with self.assertRaises(ConfigError) as caught:
            self._valid(
                report_backend="smtp", smtp_host=None, smtp_to=[], saby_login=None
            ).validate()

        message = str(caught.exception)
        self.assertIn("SMTP_HOST", message)
        self.assertIn("SABY_LOGIN", message)


class EnvFileTests(NetworkGuardTestCase):
    def test_quotes_and_comments_are_handled(self):
        from app.config import _load_env_file

        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '# комментарий\n'
                'ПУСТАЯ=\n'
                'SMTP_FROM="developer@example.com"\n'
                "SMTP_HOST='mail.local'\n"
                'PASSWORD_WITH_EQUALS=a=b=c\n'
                'без_равно\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                _load_env_file(path)
                self.assertEqual(os.environ["SMTP_FROM"], "developer@example.com")
                self.assertEqual(os.environ["SMTP_HOST"], "mail.local")
                self.assertEqual(os.environ["PASSWORD_WITH_EQUALS"], "a=b=c")
                self.assertNotIn("без_равно", os.environ)

    def test_existing_environment_wins(self):
        from app.config import _load_env_file

        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SMTP_HOST=из-файла\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"SMTP_HOST": "из-окружения"}, clear=True):
                _load_env_file(path)
                self.assertEqual(os.environ["SMTP_HOST"], "из-окружения")


if __name__ == "__main__":
    unittest.main()


class ExpenseTypeFilterTests(NetworkGuardTestCase):
    """Пустое значение должно означать «все виды», а не умолчание.

    Раньше настройка читалась через `or [умолчание]`, и «задано пустым» было
    неотличимо от «не задано»: выключить фильтр было нечем.
    """

    def test_unset_falls_back_to_the_pair_from_tz(self):
        settings = _settings_from({})

        self.assertEqual(["Фрахт", "ЖД РФ"], settings.one_s_allowed_expense_types)

    def test_empty_value_means_every_type(self):
        settings = _settings_from({"ONE_S_ALLOWED_EXPENSE_TYPES": ""})

        self.assertEqual([], settings.one_s_allowed_expense_types)

    def test_explicit_list_is_honoured(self):
        settings = _settings_from({"ONE_S_ALLOWED_EXPENSE_TYPES": "Фрахт; Портовые"})

        self.assertEqual(["Фрахт", "Портовые"], settings.one_s_allowed_expense_types)
