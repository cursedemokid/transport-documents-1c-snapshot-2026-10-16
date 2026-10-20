"""Запуск формы подтверждения одной командой.

Поднимает сервис, открывает отдельное окно браузера сразу на форме и гасит всё,
когда окно закрывают. Обычный профиль браузера не трогается: окно работает во
временном каталоге, который удаляется при выходе.

    python scripts/desktop.py

Если браузера на машине нет — например, на сервере без графики, — печатает адрес
и держит сервис до Ctrl+C.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ConfigError, Settings, load_env_files  # noqa: E402

APP_NAME = "Разноска транспортных документов"
STARTUP_TIMEOUT_SECONDS = 20
#: Страница, ради которой всё и запускается.
START_PATH = "/confirm"


class LauncherError(RuntimeError):
    pass


def browser_candidates(system: str | None = None) -> tuple[str, ...]:
    system = system or platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        return tuple(
            candidate
            for candidate in (
                str(Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe")
                if program_files_x86 else "",
                str(Path(program_files) / "Google/Chrome/Application/chrome.exe")
                if program_files else "",
                str(Path(local) / "Google/Chrome/Application/chrome.exe") if local else "",
                "msedge.exe",
                "chrome.exe",
            )
            if candidate
        )
    if system == "Darwin":
        return (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
    return (
        "google-chrome",
        "google-chrome-stable",
        "microsoft-edge",
        "microsoft-edge-stable",
        "chromium",
        "chromium-browser",
    )


def find_browser() -> str | None:
    """Путь к браузеру или None, если его нет.

    Отсутствие браузера — не ошибка: сервис живёт и на машинах без графики, и
    там достаточно напечатать адрес.
    """
    configured = os.environ.get("DESKTOP_BROWSER")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve())
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        raise LauncherError(f"Указанный в DESKTOP_BROWSER браузер не найден: {configured}")

    if platform.system() == "Linux" and not os.environ.get("DISPLAY") and not os.environ.get(
        "WAYLAND_DISPLAY"
    ):
        # Графики нет — открывать окно некуда.
        return None

    for candidate in browser_candidates():
        path = Path(candidate)
        if path.is_absolute() and path.is_file():
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def browser_command(executable: str, url: str, profile_directory: str) -> list[str]:
    """Отдельное окно без вкладок и без обычного профиля пользователя."""
    return [
        executable,
        f"--app={url}",
        f"--user-data-dir={profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-component-update",
        "--no-service-autorun",
        "--REDACTED-store=basic",
    ]


def close_browser(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def select_port(host: str, preferred: int) -> int:
    """Занятый порт не повод разбираться: берём любой свободный."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise LauncherError("Не удалось занять ни один локальный порт.")


def wait_for_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            # Поток умер на старте — почти всегда это ошибка конфигурации,
            # и она уже в логе выше.
            raise LauncherError(
                "Сервис не запустился. Смотрите сообщение выше — чаще всего "
                "это ошибка в настройках."
            )
        time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise LauncherError("Сервис не успел запуститься.")


def stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=5)


def pending_summary(settings: Settings) -> str:
    """Что сейчас ждёт подтверждения.

    Без этой строки человек открывает форму, видит запрос кода и не понимает,
    что подтверждать просто нечего.
    """
    path = Path(settings.confirm_pending_path)
    try:
        batch = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("Ожидающих подтверждения документов нет — сначала выполните прогон "
                "синхронизации, он подготовит предложения и вышлет код.")
    count = len(batch.get("items", []))
    until = str(batch.get("expires_at", ""))[:16].replace("T", " ")
    return f"Ждут решения: {count}. Код из письма действует до {until}."


def install_stop_handler() -> threading.Event:
    """Останов по сигналу, а не только по Ctrl+C.

    Ctrl+C доходит не всегда: процесс, запущенный в фоне, наследует игнорирование
    SIGINT и KeyboardInterrupt в нём не возникает вовсе. Тогда единственным
    способом остановиться остаётся `kill`, и он тоже должен гасить чисто, а не
    обрывать сервис на полуслове.
    """
    stop = threading.Event()

    def handle(signum, frame):  # noqa: ARG001 — подпись задана signal
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    return stop


def run() -> int:
    os.chdir(PROJECT_ROOT)
    load_env_files()
    try:
        settings = Settings.from_env()
        settings.validate()
    except ConfigError as error:
        raise LauncherError(f"Настройки заполнены неверно.\n{error}") from error

    # Адрес и порт берутся из настроек: на рабочей станции это localhost, а
    # на сервере форма должна быть видна с других машин.
    host = settings.app_host
    port = select_port(host, settings.app_port)
    url = f"http://service.example.invalid"

    from main import app  # импорт после chdir: приложение читает настройки

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # Свои обработчики сигналов отключаем: Ctrl+C должен доходить до нас.
    server.install_signal_handlers = lambda: None
    stop = install_stop_handler()
    thread = threading.Thread(target=server.run, name="transport-documents", daemon=True)
    thread.start()
    wait_for_server(server, thread)

    print(f"\n{APP_NAME}")
    print(f"  {pending_summary(settings)}")
    print(f"  Форма: {url}")
    if not settings.one_s_writes_enabled:
        print("  Запись в 1С выключена: подтверждение будет принято, но документы "
              "останутся неизменными.")

    browser = find_browser()
    try:
        if browser is None:
            print("  Браузер не найден — откройте адрес вручную. Ctrl+C завершает.\n")
            try:
                while thread.is_alive() and not stop.wait(0.5):
                    pass
            except KeyboardInterrupt:
                return 130
            return 0

        print("  Закройте окно браузера, чтобы остановить сервис.\n")
        with tempfile.TemporaryDirectory(prefix="transport-documents-browser-") as profile:
            process = subprocess.Popen(
                browser_command(browser, url, profile),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                # Ждём и закрытия окна, и сигнала: остановить сервис можно
                # обоими способами, и окно в этом случае закрывается вместе с ним.
                while process.poll() is None and not stop.wait(0.2):
                    pass
                if process.poll() is None:
                    close_browser(process)
                    return 143
                return process.returncode
            except KeyboardInterrupt:
                close_browser(process)
                return 130
    finally:
        stop_server(server, thread)
        print(f"{APP_NAME}: сервис остановлен.")


def main() -> None:
    try:
        raise SystemExit(run())
    except LauncherError as error:
        print(f"{APP_NAME}: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
