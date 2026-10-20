import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.confirm import router as confirm_router
from app.api.routes import router
from app.bootstrap import configure_logging, configure_socket_timeout
from app.config import ConfigError, Settings
from app.services.scheduler import DailySyncScheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Логирование настраивается до чтения настроек: иначе ошибка конфигурации
    # окажется невидимой.
    configure_logging()
    try:
        settings = Settings.from_env()
        settings.validate()
        # Планировщик строится здесь же: его конструктор разбирает DAILY_SYNC_TIME
        # и DAILY_SYNC_TIMEZONE, и эта ошибка — такая же ошибка конфигурации,
        # которая должна быть названа, а не выпасть необработанной.
        scheduler = DailySyncScheduler(settings)
    except ConfigError:
        logger.exception("Сервис не запущен из-за ошибки конфигурации")
        raise

    # Не ошибка конфигурации — сервис работоспособен, — но письмо с кодом уйдёт
    # без адреса формы, и получатель останется с кодом и без двери.
    if settings.confirm_enabled and not settings.confirm_base_url:
        logger.warning(
            "CONFIRM_BASE_URL не задан: в письме с кодом не будет ссылки на форму "
            "подтверждения. Укажите адрес, по которому она видна с рабочих мест."
        )

    # pysaby зовёт urlopen без таймаута — без общего предела ночной прогон
    # может висеть на выборке из СБИС бесконечно.
    configure_socket_timeout(settings.request_timeout_seconds)
    app.state.settings = settings
    app.state.daily_sync_scheduler = scheduler
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="Saby 1C Transport Documents Integration", lifespan=lifespan)
app.include_router(router)
app.include_router(confirm_router)


@app.get("/", tags=["service"])
async def root():
    return {
        "service": "saby-1c-transport-documents",
        "status": "ok",
    }
