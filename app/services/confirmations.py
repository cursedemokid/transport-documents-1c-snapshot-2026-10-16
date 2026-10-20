"""Партия документов, ожидающих подтверждения, и журнал решений.

Программа не записывает в 1С сама: она готовит предложения, а решение принимает
человек. Между прогоном и решением проходят часы, поэтому предложения надо
где-то держать — вместе со всем, что понадобится для записи, когда сотрудник
наконец нажмёт кнопку.

Два файла рядом с состоянием синхронизации:

* `pending.json` — текущая партия: код доступа (хэшем), срок годности и позиции.
* `confirmations.jsonl` — журнал, только дописывание. Это ответ на вопрос «кто
  разрешил запись в бухгалтерию», поэтому он не ротируется и не чистится.

Код хранится хэшем. Иначе файл состояния превращается в связку ключей: кто
прочитал его, тот и подтверждает.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.schemas.one_s import AdditionalExpenseItem
from app.schemas.saby import SabyDocument

logger = logging.getLogger(__name__)

#: Алфавит кода без символов, которые путают при переписывании с экрана.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10

#: Статусы, по которым человеку есть что решать.
CONFIRMABLE_STATUSES = frozenset({"awaiting_confirmation", "ambiguous"})


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))


def hash_code(code: str) -> str:
    """Хэш для хранения и для журнала.

    Соль не нужна и вредна: код живёт двое суток, а сравнивать хэш приходится и
    при вводе, и при разборе журнала задним числом.
    """
    return "sha256:" + hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass
class PendingItem:
    """Одно предложение: документ Saby и то, из чего выбирать."""

    key: str
    saby_document: dict[str, Any]
    candidates: list[dict[str, Any]]
    status: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "saby_document": self.saby_document,
            "candidates": self.candidates,
            "status": self.status,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingItem":
        return cls(
            key=raw["key"],
            saby_document=raw["saby_document"],
            candidates=raw.get("candidates", []),
            status=raw.get("status", ""),
            message=raw.get("message", ""),
        )

    def document(self) -> SabyDocument:
        return SabyDocument.model_validate(self.saby_document)

    def candidate(self, ref: str) -> AdditionalExpenseItem | None:
        for raw in self.candidates:
            if raw.get("ref") == ref:
                return AdditionalExpenseItem.model_validate(raw)
        return None


@dataclass
class PendingBatch:
    code_hash: str
    expires_at: datetime
    created_at: datetime
    request_id: str
    items: list[PendingItem] = field(default_factory=list)
    #: Когда кодом воспользовались. Код одноразовый: открыл сессию — погас.
    #: Потерявший сессию запрашивает новый на почту, письмо приходит за секунды.
    code_used_at: datetime | None = None

    def is_expired(self, at: datetime | None = None) -> bool:
        return (at or now()) >= self.expires_at

    def is_code_usable(self, at: datetime | None = None) -> bool:
        return self.code_used_at is None and not self.is_expired(at)

    def item(self, key: str) -> PendingItem | None:
        return next((item for item in self.items if item.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_hash": self.code_hash,
            "expires_at": self.expires_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "request_id": self.request_id,
            "items": [item.to_dict() for item in self.items],
            "code_used_at": self.code_used_at.isoformat() if self.code_used_at else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingBatch":
        return cls(
            code_hash=raw["code_hash"],
            expires_at=datetime.fromisoformat(raw["expires_at"]),
            created_at=datetime.fromisoformat(raw["created_at"]),
            request_id=raw.get("request_id", ""),
            items=[PendingItem.from_dict(item) for item in raw.get("items", [])],
            code_used_at=(
                datetime.fromisoformat(raw["code_used_at"]) if raw.get("code_used_at") else None
            ),
        )


class ConfirmationStore:
    """Хранилище партии и журнала.

    Испорченный файл партии не должен ронять прогон: худшее, что случится, —
    сотрудник не увидит предложений и документы уйдут в следующую рассылку.
    Журнал же теряться не должен, поэтому ошибка записи в него логируется
    громко.
    """

    def __init__(self, pending_path: str | Path, journal_path: str | Path):
        self._pending_path = Path(pending_path)
        self._journal_path = Path(journal_path)

    # --- партия ------------------------------------------------------------

    def load(self) -> PendingBatch | None:
        try:
            raw = json.loads(self._pending_path.read_text(encoding="utf-8"))
            return PendingBatch.from_dict(raw)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logger.warning("Не удалось прочитать партию подтверждений %s", self._pending_path)
            return None

    def save(self, batch: PendingBatch) -> None:
        self._write_pending(batch.to_dict())

    def clear(self) -> None:
        self._write_pending(None)

    def _write_pending(self, payload: dict[str, Any] | None) -> None:
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            if payload is None:
                self._pending_path.unlink(missing_ok=True)
                return
            self._pending_path.write_text(
                json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
            )
        except OSError:
            logger.warning("Не удалось сохранить партию подтверждений %s", self._pending_path)

    def open_batch(self, items: list[PendingItem], request_id: str, ttl_hours: int):
        """Создаёт партию. Возвращает (код, партия) или (None, None), если нечего.

        Код возвращается открытым ровно один раз — чтобы попасть в письмо.
        Дальше он существует только хэшем.
        """
        if not items:
            return None, None

        code = generate_code()
        batch = PendingBatch(
            code_hash=hash_code(code),
            created_at=now(),
            expires_at=now() + timedelta(hours=max(1, ttl_hours)),
            request_id=request_id,
            items=items,
        )
        self.save(batch)
        return code, batch

    def mark_code_used(self) -> None:
        """Гасит код после удачного входа: он одноразовый."""
        batch = self.load()
        if batch is None:
            return
        batch.code_used_at = now()
        self.save(batch)

    def reissue_code(self) -> str | None:
        """Выдаёт партии новый код взамен прежнего.

        Прежний восстановить нельзя: хранится только хэш — в этом и смысл
        такого хранения. Поэтому повторная отправка не «присылает тот же код»,
        а заменяет его, и старый перестаёт работать. Для сотрудника разницы
        нет, а утёкший код гаснет сам собой.
        """
        batch = self.load()
        if batch is None or batch.is_expired():
            return None
        code = generate_code()
        batch.code_hash = hash_code(code)
        batch.code_used_at = None
        self.save(batch)
        return code

    def remove_item(self, key: str) -> None:
        batch = self.load()
        if batch is None:
            return
        batch.items = [item for item in batch.items if item.key != key]
        if batch.items:
            self.save(batch)
        else:
            # Пустая партия — это отсутствие партии: код больше ничего не открывает.
            self.clear()

    # --- журнал ------------------------------------------------------------

    def record(self, event: str, **fields: Any) -> None:
        entry = {"время": now().isoformat(), "событие": event, **fields}
        try:
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self._journal_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            # Журнал — след для разбирательств, его пропажу нельзя проглатывать.
            logger.error("Не удалось записать в журнал подтверждений %s", self._journal_path)
