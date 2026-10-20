from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import ConfigError, Settings
from app.schemas.sync import SyncRunRequest
from app.services.sync_service import SyncService


logger = logging.getLogger(__name__)

#: Предел ожидания ночного прогона при остановке сервиса. `asyncio.to_thread`
#: не отменяется: без границы процесс не выходит вообще, а прогон длится минуты.
STOP_TIMEOUT_SECONDS = 10.0

#: Запасной пояс, когда в системе нет базы tzdata (типовой случай на голом
#: контейнере). Москва — рабочий пояс сервиса, остальные подставлять наугад нельзя.
_FALLBACK_ZONES = {"Europe/Moscow": timezone(timedelta(hours=3), "Europe/Moscow")}


def _parse_time(value: str) -> time:
    """Разбирает DAILY_SYNC_TIME: HH:MM или HH:MM:SS.

    Конструктор планировщика вызывается при старте безусловно — даже когда сам
    планировщик выключен, — поэтому ошибка здесь роняет весь сервис и обязана
    называть переменную и то, что в ней ожидается.
    """
    parts = (value or "").strip().split(":")
    if len(parts) not in (2, 3) or not all(part.strip().isdigit() for part in parts):
        raise ConfigError(f"DAILY_SYNC_TIME={value!r}: ожидается HH:MM или HH:MM:SS")

    hour, minute, second = (*(int(part) for part in parts), 0)[:3]
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ConfigError(f"DAILY_SYNC_TIME={value!r}: время вне диапазона суток")
    return time(hour=hour, minute=minute, second=second)


def _load_timezone(name: str):
    """Пояс для расчёта времени запуска.

    ZoneInfo бросает не только ZoneInfoNotFoundError: на пустой строке и на
    абсолютном пути это ValueError, и он пролетал мимо except, роняя старт.
    """
    key = (name or "").strip()
    if not key:
        raise ConfigError("DAILY_SYNC_TIMEZONE пуст — укажите пояс, например Europe/Moscow")
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as error:
        fallback = _FALLBACK_ZONES.get(key)
        if fallback is not None:
            logger.warning("База часовых поясов недоступна, используется смещение для %s.", key)
            return fallback
        raise ConfigError(f"DAILY_SYNC_TIMEZONE={name!r}: неизвестный часовой пояс") from error


class DailySyncScheduler:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._run_time = _parse_time(settings.daily_sync_time)
        self._timezone = _load_timezone(settings.daily_sync_timezone)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._settings.daily_sync_enabled:
            logger.info("Daily sync scheduler is disabled.")
            return
        if self._task is not None and not self._task.done():
            return
        # Запрет записи планировщик не выключает: «каждый день проверяем и шлём
        # отчёт, ничего не пишем» — дефолт настроек и ровно то, что нужно для
        # наблюдения перед включением записи. Прогон при этом принудительно
        # холостой, это делает сам SyncService.
        if not self._settings.one_s_writes_enabled:
            logger.warning(
                "ONE_S_WRITES_ENABLED=false: ночной прогон будет холостым, запись в 1С не выполняется."
            )
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="daily-sync-scheduler")
        logger.info(
            "Daily sync scheduler started: %s %s (%s).",
            self._settings.daily_sync_time,
            self._settings.daily_sync_timezone,
            "холостой прогон" if self._effective_dry_run() else "с записью в 1С",
        )

    def _effective_dry_run(self) -> bool:
        return self._settings.daily_sync_dry_run or not self._settings.one_s_writes_enabled

    async def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Гасит цикл, не дожидаясь конца прогона дольше таймаута.

        Сам прогон живёт в `asyncio.to_thread`, а такой поток не отменяется:
        `cancel()` снимает только ожидание. Без границы остановка сервиса
        превращалась в ожидание конца ночного прогона — то есть в зависание.
        """
        task, self._task = self._task, None
        if task is None:
            return
        self._stop_event.set()
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "Ночной прогон не завершился за %s с — сервис останавливается, не дожидаясь его.",
                timeout,
            )
        except asyncio.CancelledError:
            # Отмена самого цикла — ожидаемый исход, мы её и заказывали. А вот
            # отмену уже нас глотать нельзя: при остановке сервиса это превратило
            # бы штатное завершение в молчаливое игнорирование.
            if not task.cancelled():
                raise

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            delay_seconds = self._seconds_until_next_run()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay_seconds)
                break
            except TimeoutError:
                pass

            if self._stop_event.is_set():
                break

            try:
                await asyncio.to_thread(self._run_once)
            except Exception:
                logger.exception("Daily sync run failed.")

    def _seconds_until_next_run(self, now: datetime | None = None) -> float:
        current = now or datetime.now(self._timezone)
        target = datetime.combine(current.date(), self._run_time, tzinfo=self._timezone)
        if current >= target:
            target += timedelta(days=1)
        return max((target - current).total_seconds(), 0)

    def _run_once(self) -> None:
        service = SyncService(self._settings)
        service.run(
            SyncRunRequest(
                dry_run=self._effective_dry_run(),
                send_report=self._settings.daily_sync_send_report,
            )
        )
