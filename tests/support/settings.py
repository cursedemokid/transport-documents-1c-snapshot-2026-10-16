"""Настройки для тестов, не зависящие от окружения разработчика.

`Settings.from_env()` читает `.env`/`develop.env` из текущего каталога, поэтому в
тестах он подхватывал бы боевую конфигурацию — вплоть до `ONE_S_WRITES_ENABLED=true`.
Здесь окружение вычищается, а загрузка файлов отключается, так что тесты всегда
работают на значениях по умолчанию.
"""

import os
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from app.config import Settings

#: Живут столько же, сколько процесс тестов. Нужны, чтобы ни один тест не писал
#: состояние синхронизации и файлы отчётов в рабочее дерево.
_STATE_DIR = tempfile.TemporaryDirectory(prefix="sync-state-")
_REPORT_DIR = tempfile.TemporaryDirectory(prefix="sync-reports-")


def default_settings(**overrides) -> Settings:
    """Значения по умолчанию из `Settings.from_env()` без влияния окружения."""
    with (
        mock.patch("app.config.load_env_files", return_value=None),
        mock.patch.dict(os.environ, {}, clear=True),
    ):
        settings = Settings.from_env()
    return replace(settings, **overrides)


def test_settings(sync_max_attempts: int = 2, **overrides) -> Settings:
    """Настройки для прогона синхронизации.

    Отчёты никуда не уходят, а состояние и файлы отчётов пишутся во временные
    каталоги: `ReportService.save` сохраняет файл до всякой проверки backend'а,
    поэтому без подмены `report_dir` тесты засоряли рабочее дерево.
    """
    overrides.setdefault("report_backend", "none")
    overrides.setdefault("sync_state_path", str(Path(_STATE_DIR.name) / "sync_state.json"))
    # Партия подтверждений — такое же состояние на диске: без подмены тесты
    # писали бы в рабочий state/pending.json.
    overrides.setdefault("confirm_pending_path", str(Path(_STATE_DIR.name) / "pending.json"))
    overrides.setdefault("confirm_journal_path", str(Path(_STATE_DIR.name) / "confirmations.jsonl"))
    overrides.setdefault("report_dir", _REPORT_DIR.name)
    # Settings.validate() требует учётку Saby — подставляем заглушку, иначе
    # каждый тест упирался бы в проверку конфигурации, а не в своё поведение.
    overrides.setdefault("saby_login", "тест")
    overrides.setdefault("saby_REDACTED", "тест")
    # Читать из 1С сервис обязан всегда, поэтому validate() требует адрес и учётку.
    overrides.setdefault("one_s_base_url", "https://internal.internal.internal.example.invalid/hs/main")
    overrides.setdefault("one_s_username", "тест")
    overrides.setdefault("one_s_REDACTED", "тест")
    return default_settings(sync_max_attempts=sync_max_attempts, **overrides)
