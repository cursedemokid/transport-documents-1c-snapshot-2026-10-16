#!/usr/bin/env bash
#
# Установка сервиса разноски транспортных документов как системной службы.
#
#   sudo ./deploy/install.sh
#
# Повторный запуск обновляет установленное: код и зависимости заменяются,
# рабочие данные — состояние прогона, партия подтверждений, журнал решений,
# отчёты и логи — остаются на месте. Файл настроек тоже не перезаписывается.
#
# Настраивается переменными окружения:
#   TARGET   куда ставить            (по умолчанию /opt/transport-documents)
#   SERVICE  имя службы и учётки     (по умолчанию transport-documents)
#   PYTHON   интерпретатор           (по умолчанию python3)

set -euo pipefail

TARGET="${TARGET:-/opt/transport-documents}"
SERVICE="${SERVICE:-transport-documents}"
PYTHON="${PYTHON:-python3}"

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIN_PYTHON="3.11"

say() { printf '  %s\n' "$*"; }
fail() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

# --- проверки до того, как что-то менять -----------------------------------

[[ $EUID -eq 0 ]] || fail "нужны права root: sudo $0"

command -v "$PYTHON" >/dev/null || fail "интерпретатор $PYTHON не найден"

version="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" - "$MIN_PYTHON" <<'PY' || fail "нужен Python $MIN_PYTHON или новее, найден $version"
import sys
need = tuple(int(part) for part in sys.argv[1].split("."))
sys.exit(0 if sys.version_info[:2] >= need else 1)
PY

command -v systemctl >/dev/null || fail "systemd не найден — ставьте службу средствами своей системы"

echo "Установка $SERVICE"
say "источник:      $SOURCE"
say "каталог:       $TARGET"
say "интерпретатор: $PYTHON ($version)"

# --- учётная запись ---------------------------------------------------------

if id "$SERVICE" >/dev/null 2>&1; then
    say "учётка $SERVICE уже есть"
else
    # Без домашнего каталога и без возможности войти: служба, а не человек.
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE"
    say "создана учётка $SERVICE"
fi

# --- код --------------------------------------------------------------------

mkdir -p "$TARGET"
# Переносим только то, что нужно для работы. Рабочие данные и настройки не
# трогаем: при обновлении они должны пережить установку.
for item in app scripts main.py requirements.txt pyproject.toml README.md; do
    [[ -e "$SOURCE/$item" ]] || fail "в источнике нет $item — запускайте скрипт из каталога проекта"
    rm -rf "${TARGET:?}/$item"
    cp -r "$SOURCE/$item" "$TARGET/"
done
say "код обновлён"

# --- окружение Python -------------------------------------------------------

if [[ ! -x "$TARGET/.venv/bin/python" ]]; then
    "$PYTHON" -m venv "$TARGET/.venv"
    say "создано окружение .venv"
fi
"$TARGET/.venv/bin/python" -m pip install --quiet --upgrade pip
"$TARGET/.venv/bin/python" -m pip install --quiet -r "$TARGET/requirements.txt"
say "зависимости установлены"

# --- рабочие каталоги -------------------------------------------------------

for directory in state reports logs; do
    mkdir -p "$TARGET/$directory"
done
chown -R root:root "$TARGET"
# Писать сервису можно только в свои три каталога — остальное дерево для него
# доступно лишь на чтение, это же закреплено в юните через ReadWritePaths.
chown -R "$SERVICE:$SERVICE" "$TARGET/state" "$TARGET/reports" "$TARGET/logs"
say "рабочие каталоги готовы"

# --- настройки --------------------------------------------------------------

if [[ -f "$TARGET/.env" ]]; then
    say "настройки на месте, не трогаем"
else
    cp "$SOURCE/.env.example" "$TARGET/.env"
    say "создан $TARGET/.env из примера — ЗАПОЛНИТЕ ЕГО ДО ЗАПУСКА"
fi
# В файле пароли: читать может только служба.
chown "$SERVICE:$SERVICE" "$TARGET/.env"
chmod 600 "$TARGET/.env"

# --- служба -----------------------------------------------------------------

unit="/etc/systemd/system/$SERVICE.service"
sed -e "s|/opt/transport-documents|$TARGET|g" \
    -e "s|^User=transport-documents$|User=$SERVICE|" \
    -e "s|^Group=transport-documents$|Group=$SERVICE|" \
    "$SOURCE/deploy/transport-documents.service" > "$unit"
systemctl daemon-reload
say "юнит установлен: $unit"

echo
echo "Осталось:"
echo "  1. Заполнить настройки:  sudoedit $TARGET/.env"
echo "  2. Проверить связь:      sudo -u $SERVICE $TARGET/.venv/bin/python $TARGET/scripts/system_check.py"
echo "  3. Запустить:            sudo systemctl enable --now $SERVICE"
echo
echo "Состояние:  systemctl status $SERVICE"
echo "Логи:       journalctl -u $SERVICE -f"
