"""Инициализация процесса: логирование и общесетевые таймауты.

Вызывается первым делом при старте — до чтения настроек, иначе ошибка
конфигурации окажется невидимой.
"""

import logging
import logging.config
import os
import socket

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

#: Отметка, что логирование уже настроено. Нужна из-за uvicorn --reload и тестов:
#: повторный вызов не должен плодить обработчики.
_CONFIGURED = False


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def configure_logging(force: bool = False) -> None:
    """Настраивает корневой логгер.

    Без этого все `logger.info` и `logger.debug` теряются, а предупреждения
    уходят через `logging.lastResort` без времени, уровня и имени логгера —
    то есть ночной прогон нечем диагностировать.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stderr",
        }
    }
    log_file = (os.getenv("LOG_FILE") or "").strip()
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        # Ротация по суткам, а не по размеру. Прогон идёт раз в день, и вопрос
        # к логу всегда звучит как «что было четырнадцатого», а не «что было
        # в предыдущем мегабайте». Старые файлы получают суффикс с датой:
        # sync.log.2026-09-15.
        handlers["file"] = {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "formatter": "default",
            "filename": log_file,
            "when": "midnight",
            "backupCount": _int_env("LOG_BACKUP_DAYS", 30),
            "encoding": "utf-8",
            "utc": False,
        }

    logging.config.dictConfig(
        {
            "version": 1,
            # Логгеры uvicorn создаются раньше нас — отключать их нельзя.
            "disable_existing_loggers": False,
            "formatters": {"default": {"format": DEFAULT_FORMAT}},
            "handlers": handlers,
            "root": {"level": level, "handlers": list(handlers)},
        }
    )
    _CONFIGURED = True


def configure_socket_timeout(seconds: float) -> None:
    """Общий предел ожидания сокета.

    Нужен из-за pysaby: он зовёт `urllib.request.urlopen(req)` без таймаута, и
    ночной прогон может висеть на выборке из СБИС бесконечно. Наши собственные
    вызовы передают `timeout` явно и этот предел перекрывают.
    """
    if seconds and seconds > 0:
        socket.setdefaulttimeout(seconds)
