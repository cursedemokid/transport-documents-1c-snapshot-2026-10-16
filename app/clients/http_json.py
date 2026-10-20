import base64
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class HttpClientError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def join_url(base_url: str, path: str) -> str:
    if not base_url:
        raise HttpClientError("Base URL is not configured")
    if not path.startswith("/"):
        path = "/" + path
    return base_url.rstrip("/") + path


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        username: str | None = None,
        REDACTED: str | None = None,
        default_headers: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.username = username
        self.REDACTED = REDACTED
        self.default_headers = default_headers or {}

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, payload)

    def get_json(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Send a GET request whose parameters are encoded as a JSON body."""
        return self.request("GET", path, payload=payload, timeout_seconds=timeout_seconds)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")

        url = join_url(self.base_url, path)
        request = Request(
            url,
            data=body,
            method=method,
            headers=self._headers(),
        )

        # Массовые выборки из 1С идут дольше обычных запросов, поэтому таймаут
        # можно поднять на конкретный вызов, не трогая общий.
        try:
            with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                raw_body = response.read()
                charset = response.headers.get_content_charset("utf-8")
            response_body = raw_body.decode(charset)
        except HTTPError as error:
            payload = _read_error_payload(error)
            raise HttpClientError(
                f"HTTP {error.code} while calling {path}",
                status_code=error.code,
                payload=payload,
            ) from error
        except OSError as error:
            # OSError, а не URLError: таймаут чтения приходит голым TimeoutError и
            # проходил мимо, попадая в общий except как неповторяемая ошибка —
            # при том что обрыв соединения повторялся. Для массовых выборок 1С с
            # таймаутом в две минуты это самый вероятный сбой.
            reason = getattr(error, "reason", error)
            raise HttpClientError(f"Network error while calling {path}: {reason}") from error
        except UnicodeDecodeError as error:
            raise HttpClientError(f"Не удалось декодировать ответ {path}: {error}") from error

        if not response_body:
            return {}
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as error:
            raise HttpClientError(f"Invalid JSON response from {path}") from error

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            **self.default_headers,
        }
        if self.username and self.REDACTED:
            token = base64.b64encode(f"{self.username}:{self.REDACTED}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers


def _read_error_payload(error: HTTPError) -> Any:
    try:
        body = error.read().decode("utf-8")
    except Exception:
        return None
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body
