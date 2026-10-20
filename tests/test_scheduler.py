import asyncio
import unittest
from datetime import datetime, time, timedelta, timezone

from app.clients.one_s import OneSClient
from app.config import ConfigError
from app.schemas.one_s_wire import UpdateIncomingRequisitesRequest
from app.services.scheduler import DailySyncScheduler, _load_timezone, _parse_time
from tests.support.no_network import AsyncNetworkGuardTestCase, NetworkGuardTestCase
from tests.support.settings import test_settings

MOSCOW = timezone(timedelta(hours=3))


class _DetectingHttpClient:
    """Фиксирует любую попытку выйти в сеть."""

    def __init__(self):
        self.called = False

    def post(self, *args, **kwargs):
        self.called = True
        raise AssertionError("HTTP POST не должен выполняться при запрещённой записи")

    def get_json(self, *args, **kwargs):
        self.called = True
        raise AssertionError("Сетевой вызов не ожидался")


class WriteGuardTests(NetworkGuardTestCase):
    def test_guard_blocks_before_any_network_call(self):
        client = OneSClient(test_settings(one_s_writes_enabled=False))
        client._http = _DetectingHttpClient()
        request = UpdateIncomingRequisitesRequest(
            document_id="7e9061fe-d89a-55c4-a8c8-495cf7b0050b",
            incoming_number="4726385",
            incoming_date=datetime(2026, 8, 17).date(),
        )

        with self.assertRaises(RuntimeError) as caught:
            client.update_incoming_requisites(request)

        self.assertIn("ONE_S_WRITES_ENABLED", str(caught.exception))
        self.assertFalse(client._http.called)


class ScheduleTimeTests(NetworkGuardTestCase):
    """Конструктор вызывается при старте даже с выключенным планировщиком.

    Поэтому разбор обязан быть тотальным: любое непонятое значение должно
    становиться внятной ошибкой конфигурации, а не необработанным исключением
    где-то в глубине старта.
    """

    def test_hh_mm_and_hh_mm_ss_are_accepted(self):
        self.assertEqual(time(17, 0), _parse_time("17:00"))
        self.assertEqual(time(17, 0, 30), _parse_time("17:00:30"))
        self.assertEqual(time(7, 5), _parse_time(" 07:05 "))

    def test_garbage_is_a_config_error(self):
        for value in ("", "   ", "17", "17:00:00:00", "вечером", "17:мм", "17-00", None):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    _parse_time(value)
                self.assertIn("DAILY_SYNC_TIME", str(caught.exception))

    def test_out_of_range_time_is_rejected(self):
        for value in ("24:00", "17:60", "17:00:60", "99:99"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    _parse_time(value)

    def test_known_timezone_is_loaded(self):
        self.assertIsNotNone(_load_timezone("Europe/Moscow"))

    def test_empty_timezone_says_it_is_empty(self):
        """Регрессия: на пустой строке ZoneInfo бросает ValueError мимо except."""
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    _load_timezone(value)
                self.assertIn("пуст", str(caught.exception))

    def test_unknown_timezone_is_a_config_error(self):
        for value in ("/etc/localtime", "Europe/Нигде", "МСК"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    _load_timezone(value)
                self.assertIn("DAILY_SYNC_TIMEZONE", str(caught.exception))


class SchedulerTests(NetworkGuardTestCase):
    def test_stays_disabled_when_not_enabled(self):
        scheduler = DailySyncScheduler(
            test_settings(daily_sync_enabled=False, one_s_writes_enabled=True)
        )

        scheduler.start()

        self.assertIsNone(scheduler._task)

    def test_waits_until_configured_time(self):
        scheduler = DailySyncScheduler(test_settings(daily_sync_time="17:00"))
        now = datetime(2026, 8, 20, 16, 30, tzinfo=MOSCOW)

        self.assertEqual(30 * 60, scheduler._seconds_until_next_run(now))

    def test_moves_to_next_day_after_configured_time(self):
        scheduler = DailySyncScheduler(test_settings(daily_sync_time="17:00"))
        now = datetime(2026, 8, 20, 17, 1, tzinfo=MOSCOW)

        self.assertEqual((23 * 60 + 59) * 60, scheduler._seconds_until_next_run(now))


class SchedulerLifecycleTests(AsyncNetworkGuardTestCase):
    async def test_runs_without_writes_forcing_a_dry_run(self):
        """«Каждый день проверяем и шлём отчёт, ничего не пишем» — дефолт настроек.

        Раньше планировщик при ONE_S_WRITES_ENABLED=false просто не запускался,
        и этот режим был недостижим.
        """
        scheduler = DailySyncScheduler(
            test_settings(
                daily_sync_enabled=True, one_s_writes_enabled=False, daily_sync_dry_run=False
            )
        )
        self.addAsyncCleanup(scheduler.stop)

        scheduler.start()

        self.assertIsNotNone(scheduler._task)
        self.assertTrue(scheduler._effective_dry_run())

    async def test_stop_does_not_wait_for_a_hung_run(self):
        """`asyncio.to_thread` не отменяется: без границы сервис не выходит вообще.

        Задача здесь переживает cancel() намеренно — именно так ведёт себя цикл,
        застрявший на `to_thread`: отмена снимает ожидание, но не поток, и задача
        остаётся висеть. Просто отменяемая задача этот случай не воспроизводит.
        """
        released = asyncio.Event()
        self.addCleanup(released.set)
        cancels = 0

        async def _survives_cancel():
            nonlocal cancels
            while True:
                try:
                    await released.wait()
                    return
                except asyncio.CancelledError:
                    cancels += 1
                    # Отмену от stop() переживаем, повторную — нет: иначе упавший
                    # тест оставит задачу, которую не может снять и сам runner.
                    if cancels > 1:
                        raise

        scheduler = DailySyncScheduler(test_settings(daily_sync_enabled=True))
        task = asyncio.create_task(_survives_cancel())
        scheduler._task = task
        await asyncio.sleep(0)  # дать задаче дойти до await — иначе cancel() снимет её сразу

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(scheduler.stop(timeout=0.05), timeout=2)
        elapsed = loop.time() - started

        # Важно именно время: без границы stop() ждал бы конца прогона, то есть
        # минуты. Факт возврата сам по себе ничего не доказывает.
        self.assertLess(elapsed, 1)
        self.assertIsNone(scheduler._task)
        self.assertFalse(task.done())

        released.set()
        await task

    async def test_stop_propagates_its_own_cancellation(self):
        """Отменили нас, а не прогон: глотать такую отмену при остановке нельзя."""
        released = asyncio.Event()
        self.addCleanup(released.set)
        cancels = 0

        async def _survives_cancel():
            nonlocal cancels
            while True:
                try:
                    await released.wait()
                    return
                except asyncio.CancelledError:
                    cancels += 1
                    # Отмену от stop() переживаем, повторную — нет: иначе упавший
                    # тест оставит задачу, которую не может снять и сам runner.
                    if cancels > 1:
                        raise

        scheduler = DailySyncScheduler(test_settings(daily_sync_enabled=True))
        task = asyncio.create_task(_survives_cancel())
        scheduler._task = task
        await asyncio.sleep(0)

        stopping = asyncio.create_task(scheduler.stop(timeout=30))
        await asyncio.sleep(0)
        stopping.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await stopping

        released.set()
        await task

    async def test_stop_is_idempotent(self):
        scheduler = DailySyncScheduler(test_settings(daily_sync_enabled=True))
        scheduler.start()

        await scheduler.stop()
        await scheduler.stop()

        self.assertIsNone(scheduler._task)

    async def test_stop_returns_quickly_for_an_idle_loop(self):
        scheduler = DailySyncScheduler(test_settings(daily_sync_enabled=True))
        scheduler.start()
        await asyncio.sleep(0)

        await asyncio.wait_for(scheduler.stop(), timeout=2)

        self.assertIsNone(scheduler._task)


if __name__ == "__main__":
    unittest.main()
