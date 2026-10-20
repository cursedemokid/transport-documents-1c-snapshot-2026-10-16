"""Память о последнем обработанном периоде.

Окно синхронизации отсчитывается от сегодняшнего дня, поэтому пропуск запусков
дольше `SYNC_WINDOW_DAYS` оставлял бы в выборке дыру, в которую документы больше
никогда не попадут.

Состояние умеет только **расширять** окно назад и никогда не сдвигает его вперёд:
при потерянном или испорченном файле поведение возвращается к обычному окну, а
худшее, что может случиться, — повторная обработка уже разнесённых документов.
Она безопасна: такие документы распознаются как уже заполненные.
"""

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


class SyncStateStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)

    def load_last_processed_date(self) -> date | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            value = raw.get("last_processed_date")
            return date.fromisoformat(value) if value else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError, json.JSONDecodeError):
            # Состояние — подсказка, а не источник истины: испорченный файл не
            # должен ломать прогон.
            logger.warning("Не удалось прочитать состояние синхронизации %s", self._path)
            return None

    def save_last_processed_date(self, value: date) -> None:
        previous = self.load_last_processed_date()
        if previous is not None and previous >= value:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"last_processed_date": value.isoformat()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Не удалось сохранить состояние синхронизации %s", self._path)
