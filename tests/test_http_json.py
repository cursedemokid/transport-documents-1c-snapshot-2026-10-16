"""Транспорт HTTP: ветки ошибок, кодировка, заголовки.

Раньше тут был один тест на счастливый путь, а в продуктиве происходят как раз
остальные ветки.
"""

import json
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.clients.http_json import HttpClientError, JsonHttpClient, join_url
from tests.support.no_network import NetworkGuardTestCase


class _Response:
    def __init__(self, body: bytes, charset: str | None = "utf-8"):
        self._body = body
        self.headers = Message()
        if charset:
            self.headers["Content-Type"] = f"application/json; charset={charset}"

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(**overrides) -> JsonHttpClient:
    params = {"base_url": "https://internal.internal.internal.example.invalid/base", "timeout_seconds": 30}
    params.update(overrides)
    return JsonHttpClient(**params)


class RequestShapeTests(NetworkGuardTestCase):
    def test_get_json_sends_body_without_query_string(self):
        payload = {"periodBegin": "20260101", "periodEnd": "20270101", "confirmed": True}

        with patch("app.clients.http_json.urlopen", return_value=_Response(b'{"code":200}')) as opener:
            result = _client().get_json("/data1C/docaddexpenses", payload)

        request = opener.call_args.args[0]
        self.assertEqual("GET", request.get_method())
        self.assertEqual("https://internal.internal.internal.example.invalid/base/data1C/docaddexpenses", request.full_url)
        self.assertEqual(payload, json.loads(request.data.decode("utf-8")))
        self.assertEqual({"code": 200}, result)

    def test_per_request_timeout_overrides_default(self):
        with patch("app.clients.http_json.urlopen", return_value=_Response(b"{}")) as opener:
            _client(timeout_seconds=30).get_json("/p", {}, timeout_seconds=120)

        self.assertEqual(120, opener.call_args.kwargs["timeout"])

    def test_basic_auth_header_is_utf8(self):
        """Логин 1С кириллический — кодировка заголовка тут не формальность."""
        import base64

        client = _client(username="обмен", REDACTED="пароль")
        with patch("app.clients.http_json.urlopen", return_value=_Response(b"{}")) as opener:
            client.get_json("/p", {})

        header = opener.call_args.args[0].get_header("Authorization")
        self.assertEqual("обмен:пароль", base64.b64decode(header.split()[1]).decode("utf-8"))

    def test_missing_base_url_is_reported(self):
        with self.assertRaises(HttpClientError):
            join_url("", "/p")


class ErrorBranchTests(NetworkGuardTestCase):
    def _fails_with(self, error) -> HttpClientError:
        with patch("app.clients.http_json.urlopen", side_effect=error):
            with self.assertRaises(HttpClientError) as caught:
                _client().get_json("/p", {})
        return caught.exception

    def test_read_timeout_is_wrapped(self):
        """Регрессия: голый TimeoutError проходил мимо и не ретраился."""
        error = self._fails_with(TimeoutError("read timed out"))

        self.assertIn("/p", str(error))

    def test_connection_errors_are_wrapped(self):
        for error in (URLError("недоступен"), ConnectionResetError("сброс"), OSError("сеть")):
            with self.subTest(error=type(error).__name__):
                self.assertIsInstance(self._fails_with(error), HttpClientError)

    def test_http_error_keeps_status_and_json_payload(self):
        body = json.dumps({"code": 500, "data": "текст исключения"}, ensure_ascii=False)
        error = self._fails_with(
            HTTPError("https://service.example.invalid/p", 500, "Server Error", Message(), _fake_body(body))
        )

        self.assertEqual(500, error.status_code)
        self.assertEqual("текст исключения", error.payload["data"])

    def test_http_error_keeps_text_payload(self):
        error = self._fails_with(
            HTTPError("https://service.example.invalid/p", 404, "Not Found", Message(), _fake_body("не json"))
        )

        self.assertEqual("не json", error.payload)

    def test_http_error_survives_unreadable_body(self):
        class _Unreadable:
            def read(self):
                raise OSError("тело не читается")

            def close(self):
                pass

        error = self._fails_with(
            HTTPError("https://service.example.invalid/p", 502, "Bad Gateway", Message(), _Unreadable())
        )

        self.assertEqual(502, error.status_code)
        self.assertIsNone(error.payload)


class ResponseBodyTests(NetworkGuardTestCase):
    def test_empty_body_is_empty_dict(self):
        with patch("app.clients.http_json.urlopen", return_value=_Response(b"")):
            self.assertEqual({}, _client().get_json("/p", {}))

    def test_broken_json_is_reported(self):
        with patch("app.clients.http_json.urlopen", return_value=_Response("{не json".encode("utf-8"))):
            with self.assertRaises(HttpClientError):
                _client().get_json("/p", {})

    def test_charset_from_header_is_honoured(self):
        """1С может ответить в cp1251 — жёсткий utf-8 давал UnicodeDecodeError."""
        body = json.dumps({"Организация": "Ромашка"}, ensure_ascii=False).encode("cp1251")

        with patch("app.clients.http_json.urlopen", return_value=_Response(body, "windows-1251")):
            result = _client().get_json("/p", {})

        self.assertEqual("Ромашка", result["Организация"])

    def test_undecodable_body_becomes_client_error(self):
        with patch("app.clients.http_json.urlopen", return_value=_Response(b"\xff\xfe", "utf-8")):
            with self.assertRaises(HttpClientError) as caught:
                _client().get_json("/p", {})

        self.assertIn("декодировать", str(caught.exception))


def _fake_body(text: str):
    class _Body:
        # HTTPError оборачивает fp в закрывалку временных файлов и зовёт close().
        def read(self):
            return text.encode("utf-8")

        def close(self):
            pass

    return _Body()


if __name__ == "__main__":
    unittest.main()
