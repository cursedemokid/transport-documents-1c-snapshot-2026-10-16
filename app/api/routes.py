"""HTTP-слой. Сам прогон синхронный и долгий, поэтому здесь его только охраняют.

Три вещи, ради которых этот модуль вообще нетривиален: прогон не должен держать
event loop, двух одновременных прогонов быть не может, и с момента включения
записи запрос `{"dry_run": false}` меняет продуктивные данные — значит, он обязан
быть аутентифицирован.
"""

import logging
import secrets
import threading

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import Settings
from app.schemas.sync import SyncRunRequest
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)

router = APIRouter()

#: Прогон читает и пишет общее состояние (state/sync_state.json, кэш заказов),
#: поэтому параллельные запуски недопустимы. Блокировка неблокирующая: второй
#: запрос должен получить внятный отказ, а не молча висеть до конца первого.
_run_lock = threading.Lock()


@router.get("/health", tags=["service"])
async def health():
    return {"status": "ok"}


def _settings(request: Request) -> Settings:
    """Настройки собираются один раз при старте, а не на каждый запрос."""
    settings = getattr(request.app.state, "settings", None)
    return settings if settings is not None else Settings.from_env()


def _require_api_key(settings: Settings, provided: str | None) -> None:
    if not settings.sync_api_key:
        if settings.one_s_writes_enabled:
            # До этого места дойти не должно: Settings.validate() ловит связку
            # при старте. Но если сервис подняли в обход — отказ, а не запись.
            raise HTTPException(
                status_code=503,
                detail="Запись в 1С включена, но SYNC_API_KEY не задан — запуск запрещён.",
            )
        return
    if not provided or not secrets.compare_digest(provided, settings.sync_api_key):
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий X-API-Key.")


# `def`, а не `async def`: прогон синхронный и идёт минутами. В async-обработчике
# он держал бы event loop, и сервис переставал отвечать даже на /health. FastAPI
# синхронный обработчик уводит в пул потоков сам.
@router.post("/sync/run", tags=["sync"])
def run_sync(
    request: SyncRunRequest,
    http_request: Request,
    x_api_key: str | None = Header(default=None),
):
    settings = _settings(http_request)
    _require_api_key(settings, x_api_key)

    if not _run_lock.acquire(blocking=False):
        logger.warning("Отклонён параллельный запуск синхронизации.")
        raise HTTPException(status_code=409, detail="Синхронизация уже выполняется.")
    try:
        response = SyncService(settings).run(request)
    finally:
        _run_lock.release()
    return response.model_dump(mode="json")
