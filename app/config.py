import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class ConfigError(RuntimeError):
    """Настройка задана неверно. Полурабочий сервис хуже упавшего."""


def load_env_files() -> Path | None:
    for path in (Path(".env"), Path("develop.env")):
        if path.exists():
            _load_env_file(path)
            return path
    return None


def _load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (key not in os.environ or os.environ[key] == ""):
            os.environ[key] = value


def _get_set(name: str, default: str) -> frozenset[str]:
    """Список через запятую. Пустая строка — осознанное «без фильтра»."""
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise ConfigError(f"{name}={value!r}: ожидается целое число") from None


_TRUE_VALUES = {"1", "true", "yes", "on", "да"}
_FALSE_VALUES = {"0", "false", "no", "off", "нет"}


def _get_bool(*names: str, default: bool) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is None or not value.strip():
            continue
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
        # Раньше нераспознанное значение молча означало False: SMTP_USE_TLS="True "
        # с хвостовым пробелом выключал шифрование.
        raise ConfigError(
            f"{name}={value!r}: ожидается да/нет "
            f"({', '.join(sorted(_TRUE_VALUES))} или {', '.join(sorted(_FALSE_VALUES))})"
        )
    return default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        raise ConfigError(
            f"{name}={value!r}: ожидается число с точкой (например 0.01)"
        ) from None


def _get_date(name: str, default: str) -> date:
    value = os.getenv(name)
    if value is None or not value.strip():
        value = default
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ConfigError(f"{name}={value!r}: ожидается дата вида ГГГГ-ММ-ДД") from None


def _get_csv(*names: str) -> list[str]:
    value = ""
    for name in names:
        value = os.getenv(name, "")
        if value:
            break
    return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]


def _get_list_or_default(name: str, default: list[str]) -> list[str]:
    """Список через запятую, где пустая строка — осознанное «без фильтра».

    Отличается от `_get_csv` тем, что различает «переменная не задана» и
    «задана пустой». Через `or default` это было неразличимо, и выключить
    фильтр совсем было нечем: пустое значение молча возвращало умолчание.
    """
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]


def _get_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _get_report_backend() -> str:
    value = os.getenv("REPORT_BACKEND")
    if value:
        return _normalize_report_backend(value)

    email_backend = os.getenv("EMAIL_BACKEND", "")
    if email_backend:
        return _normalize_report_backend(email_backend)

    return "smtp"


#: Точные значения проверяются раньше подстрок. Иначе "smtp+console" попадал под
#: проверку на подстроку "console", резолвился в "console", и письмо не уходило —
#: притом что .env.example документирует это значение как рабочее.
_REPORT_BACKEND_ALIASES = {
    "both": "both", "all": "both", "smtp+console": "both", "console+smtp": "both",
    "console": "console",
    "none": "none", "off": "none", "disabled": "none", "dummy": "none", "locmem": "none",
    "smtp": "smtp", "email": "smtp",
}


def _normalize_report_backend(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in _REPORT_BACKEND_ALIASES:
        return _REPORT_BACKEND_ALIASES[normalized]

    # Django-имена вида django.core.mail.backends.console.EmailBackend
    for marker, backend in (
        ("console", "console"),
        ("dummy", "none"),
        ("locmem", "none"),
        ("smtp", "smtp"),
    ):
        if marker in normalized:
            return backend

    raise ConfigError(
        f"REPORT_BACKEND={value!r}: допустимы "
        f"{', '.join(sorted(set(_REPORT_BACKEND_ALIASES)))}"
    )


@dataclass(frozen=True)
class Settings:
    saby_base_url: str
    saby_login: str | None
    saby_REDACTED: str | None
    saby_page_size: int
    saby_max_pages: int
    #: Типы документов реестра СБИС, которые нас касаются. Пусто — все.
    saby_document_types: frozenset[str]
    #: Повторы чтения одной страницы реестра. Чтение идемпотентно, а обрыв
    #: TLS на одной странице из тридцати валил весь прогон.
    saby_page_retries: int
    #: Куда pysaby кладёт свою сессию. По умолчанию библиотека пишет её
    #: внутрь site-packages — там она теряется при переустановке и не
    #: пишется вовсе, если окружение доступно только на чтение.
    saby_session_path: str
    saby_retry_delay_seconds: float

    one_s_base_url: str
    one_s_username: str | None
    one_s_REDACTED: str | None
    one_s_writes_enabled: bool
    #: Разрешить прогону записывать самостоятельно, без подтверждения
    #: человека. Пока выключено, ночной прогон только готовит предложения.
    one_s_auto_write_enabled: bool
    #: Ключ для POST /sync/run. Без него запуск с записью в 1С запрещён.
    sync_api_key: str | None
    #: Где слушает сервис. Для формы подтверждения с рабочих мест нужен
    #: адрес, видимый в сети, а не localhost.
    app_host: str
    app_port: int
    sync_state_path: str
    #: Партия, ожидающая подтверждения, и журнал решений.
    confirm_pending_path: str
    confirm_journal_path: str
    confirm_enabled: bool
    confirm_base_url: str
    confirm_code_ttl_hours: int
    confirm_session_minutes: int
    confirm_max_attempts: int
    confirm_lockout_minutes: int
    confirm_resend_interval_minutes: int
    one_s_allowed_warehouse_groups: list[str]
    #: Виды доп. расходов, которые берутся в работу. Пустой список — все.
    one_s_allowed_expense_types: list[str]
    one_s_auto_pickup_row_name: str
    one_s_auto_pickup_fields: list[str]
    one_s_bulk_chunk_days: int
    one_s_bulk_max_chunks: int
    one_s_bulk_timeout_seconds: float
    one_s_order_date_slack_days: int

    match_min_score: int
    match_score_margin: int
    match_name_similarity: float
    match_date_window_days: int
    expense_lookback_days: int
    expense_lookahead_days: int
    allow_completing_partial_requisites: bool

    sync_window_days: int
    sync_max_lookback_days: int
    sync_max_attempts: int
    sync_start_date: date
    amount_tolerance: float
    default_organization_name: str | None

    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_REDACTED: str | None
    smtp_from: str | None
    smtp_to: list[str]
    smtp_cc: list[str]
    smtp_bcc: list[str]
    smtp_use_tls: bool
    smtp_use_ssl: bool
    smtp_subject_prefix: str
    smtp_attach_report: bool
    smtp_max_attachment_mb: int
    smtp_timeout_seconds: float
    report_format: str
    report_dir: str
    #: Сколько файлов отчётов держать в report_dir. 0 — не чистить.
    report_keep_files: int
    report_backend: str

    daily_sync_enabled: bool
    daily_sync_time: str
    daily_sync_timezone: str
    daily_sync_dry_run: bool
    daily_sync_send_report: bool

    request_timeout_seconds: float

    def validate(self) -> None:
        """Проверяет связки между настройками и сообщает обо ВСЕХ проблемах сразу.

        Отдельные значения уже разобраны строгими геттерами; здесь ловится то,
        что видно только в сочетании — например, включённая отправка почты при
        ненастроенном SMTP.
        """
        problems: list[str] = []

        if self.report_backend in {"smtp", "both"}:
            if not self.smtp_host:
                problems.append("REPORT_BACKEND требует отправки почты, но SMTP_HOST не задан")
            if not self.smtp_to:
                problems.append(
                    "REPORT_BACKEND требует отправки почты, но получатели не заданы "
                    "(ERROR_EMAIL_TO / SMTP_TO)"
                )

        # Читать из 1С нужно всегда, независимо от того, разрешена ли запись.
        # Без адреса сервис исправно стартует и молча превращает каждый документ
        # в техническую ошибку «Base URL is not configured» — отчёт полон сбоев,
        # а причина видна только тому, кто откроет отчёт.
        if not self.one_s_base_url:
            problems.append("не задан ONE_S_BASE_URL — читать из 1С нечем")
        if not self.one_s_username or not self.one_s_REDACTED:
            problems.append("не заданы ONE_S_USERNAME и ONE_S_PASSWORD")

        if self.one_s_writes_enabled and not self.sync_api_key:
            # С этого момента неаутентифицированный POST /sync/run с dry_run=false
            # меняет продуктивные данные.
            problems.append(
                "ONE_S_WRITES_ENABLED=true при пустом SYNC_API_KEY — "
                "запуск синхронизации был бы открыт всем"
            )

        if not self.saby_login or not self.saby_REDACTED:
            problems.append("не заданы SABY_LOGIN и SABY_PASSWORD — документы читать нечем")

        if self.sync_start_date > date.today():
            problems.append(
                f"SYNC_START_DATE={self.sync_start_date} в будущем — выборка всегда будет пустой"
            )

        if self.sync_window_days < 1:
            problems.append(f"SYNC_WINDOW_DAYS={self.sync_window_days}: должно быть не меньше 1")
        if self.smtp_max_attachment_mb < 1:
            problems.append(
                f"SMTP_MAX_ATTACHMENT_MB={self.smtp_max_attachment_mb}: "
                "при таком значении вложение не попадёт ни в одно письмо"
            )

        if problems:
            raise ConfigError("Ошибки конфигурации:\n  - " + "\n  - ".join(problems))

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_files()
        return cls(
            saby_base_url=os.getenv("SABY_BASE_URL", "").rstrip("/"),
            saby_login=os.getenv("SABY_LOGIN"),
            saby_REDACTED=os.getenv("SABY_PASSWORD"),
            saby_page_size=_get_int("SABY_PAGE_SIZE", 200),
            # Реестр СБИС игнорирует фильтр по датам, поэтому выборку надо
            # ограничивать здесь: иначе она листает весь реестр.
            saby_max_pages=_get_int("SABY_MAX_PAGES", 30),
            saby_document_types=_get_set("SABY_DOCUMENT_TYPES", "ДокОтгрВх"),
            saby_page_retries=_get_int("SABY_PAGE_RETRIES", 3),
            saby_session_path=os.getenv("SABY_SESSION_PATH", "state/saby_session.db"),
            saby_retry_delay_seconds=_get_float("SABY_RETRY_DELAY_SECONDS", 2.0),
            one_s_base_url=os.getenv("ONE_S_BASE_URL", "").rstrip("/"),
            one_s_username=os.getenv("ONE_S_USERNAME"),
            one_s_REDACTED=os.getenv("ONE_S_PASSWORD"),
            one_s_writes_enabled=_get_bool("ONE_S_WRITES_ENABLED", default=False),
            one_s_auto_write_enabled=_get_bool("ONE_S_AUTO_WRITE_ENABLED", default=False),
            sync_api_key=(os.getenv("SYNC_API_KEY") or "").strip() or None,
            app_host=os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            app_port=_get_int("APP_PORT", 8000),
            sync_state_path=os.getenv("SYNC_STATE_PATH", "state/sync_state.json"),
            confirm_pending_path=os.getenv("CONFIRM_PENDING_PATH", "state/pending.json"),
            confirm_journal_path=os.getenv("CONFIRM_JOURNAL_PATH", "state/confirmations.jsonl"),
            confirm_enabled=_get_bool("CONFIRM_ENABLED", default=True),
            confirm_base_url=os.getenv("CONFIRM_BASE_URL", "").rstrip("/"),
            confirm_code_ttl_hours=_get_int("CONFIRM_CODE_TTL_HOURS", 48),
            confirm_session_minutes=_get_int("CONFIRM_SESSION_MINUTES", 30),
            # Пороги намеренно мягкие. От подбора защищает длина кода: 10 знаков
            # из 32-буквенного алфавита — это 10^15 вариантов, их не перебрать.
            # А счётчик ведётся по выходному адресу, и за NAT он общий на весь
            # офис: жёсткая пауза закрывала бы форму всем из-за чужих опечаток.
            confirm_max_attempts=_get_int("CONFIRM_MAX_ATTEMPTS", 10),
            confirm_lockout_minutes=_get_int("CONFIRM_LOCKOUT_MINUTES", 5),
            confirm_resend_interval_minutes=_get_int("CONFIRM_RESEND_INTERVAL_MINUTES", 5),
            one_s_allowed_warehouse_groups=(
                _get_csv("ONE_S_ALLOWED_WAREHOUSE_GROUPS") or ["ОСНОВНЫЕ"]
            ),
            # Пустое значение = обрабатываются ВСЕ виды доп. расходов.
            one_s_allowed_expense_types=_get_list_or_default(
                "ONE_S_ALLOWED_EXPENSE_TYPES", ["Фрахт", "ЖД РФ"]
            ),
            one_s_auto_pickup_row_name=os.getenv(
                "ONE_S_AUTO_PICKUP_ROW_NAME", "Дата автовывоза со станции/порта = Пост."
            ),
            # Пусто по умолчанию: техническое имя поля 1С пока не назвала,
            # а угадывать его нельзя — молча не совпадёт и проверка не включится.
            one_s_auto_pickup_fields=_get_csv("ONE_S_AUTO_PICKUP_FIELDS"),
            one_s_bulk_chunk_days=_get_int("ONE_S_BULK_CHUNK_DAYS", 92),
            one_s_bulk_max_chunks=_get_int("ONE_S_BULK_MAX_CHUNKS", 8),
            one_s_bulk_timeout_seconds=_get_float("ONE_S_BULK_TIMEOUT_SECONDS", 120.0),
            one_s_order_date_slack_days=_get_int("ONE_S_ORDER_DATE_SLACK_DAYS", 1),
            match_min_score=_get_int("MATCH_MIN_SCORE", 40),
            match_score_margin=_get_int("MATCH_SCORE_MARGIN", 20),
            match_name_similarity=_get_float("MATCH_NAME_SIMILARITY", 0.85),
            match_date_window_days=_get_int("MATCH_DATE_WINDOW_DAYS", 45),
            expense_lookback_days=_get_int("EXPENSE_LOOKBACK_DAYS", 45),
            expense_lookahead_days=_get_int("EXPENSE_LOOKAHEAD_DAYS", 14),
            allow_completing_partial_requisites=_get_bool(
                "ALLOW_COMPLETING_PARTIAL_REQUISITES", default=True
            ),
            sync_window_days=_get_int("SYNC_WINDOW_DAYS", 14),
            sync_max_lookback_days=_get_int("SYNC_MAX_LOOKBACK_DAYS", 180),
            sync_max_attempts=_get_int("SYNC_MAX_ATTEMPTS", 2),
            sync_start_date=_get_date("SYNC_START_DATE", "2026-01-01"),
            amount_tolerance=_get_float("AMOUNT_TOLERANCE", 0.01),
            default_organization_name=os.getenv("DEFAULT_ORGANIZATION_NAME"),
            smtp_host=_get_first("SMTP_HOST", "EMAIL_HOST"),
            smtp_port=_get_int("SMTP_PORT", _get_int("EMAIL_PORT", 587)),
            smtp_username=_get_first("SMTP_USERNAME", "EMAIL_HOST_USER"),
            smtp_REDACTED=_get_first("SMTP_PASSWORD", "EMAIL_HOST_PASSWORD"),
            smtp_from=_get_first("SMTP_FROM", "SMTP_FROM_EMAIL", "DEFAULT_FROM_EMAIL", "EMAIL_HOST_USER"),
            smtp_to=_get_csv("ERROR_EMAIL_TO", "SMTP_TO_EMAILS", "EMAIL_TO", "SMTP_TO"),
            smtp_cc=_get_csv("SMTP_CC_EMAILS", "EMAIL_CC", "SMTP_CC"),
            smtp_bcc=_get_csv("SMTP_BCC_EMAILS", "EMAIL_BCC", "SMTP_BCC"),
            smtp_use_tls=_get_bool("SMTP_USE_TLS", "EMAIL_USE_TLS", default=True),
            smtp_use_ssl=_get_bool("SMTP_USE_SSL", "EMAIL_USE_SSL", default=False),
            smtp_subject_prefix=os.getenv("SMTP_SUBJECT_PREFIX", "[Saby/1C]"),
            smtp_attach_report=_get_bool("SMTP_ATTACH_REPORT", default=True),
            smtp_max_attachment_mb=_get_int("SMTP_MAX_ATTACHMENT_MB", 10),
            smtp_timeout_seconds=_get_float("SMTP_TIMEOUT_SECONDS", 30.0),
            report_format=os.getenv("SMTP_REPORT_FORMAT", os.getenv("REPORT_FORMAT", "xlsx")).lower(),
            report_dir=os.getenv("REPORT_DIR", "reports"),
            report_keep_files=_get_int("REPORT_KEEP_FILES", 90),
            report_backend=_get_report_backend(),
            daily_sync_enabled=_get_bool("DAILY_SYNC_ENABLED", default=False),
            daily_sync_time=os.getenv("DAILY_SYNC_TIME", "17:00"),
            daily_sync_timezone=os.getenv("DAILY_SYNC_TIMEZONE", "Europe/Moscow"),
            daily_sync_dry_run=_get_bool("DAILY_SYNC_DRY_RUN", default=True),
            daily_sync_send_report=_get_bool("DAILY_SYNC_SEND_REPORT", default=True),
            request_timeout_seconds=_get_float("REQUEST_TIMEOUT_SECONDS", 30.0),
        )
