#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Ejecuta este instalador con sudo." >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/eva-telegram"
CONFIG_DIR="/etc/eva-telegram"
DATA_DIR="/var/lib/eva-telegram"
SERVICE_USER="eva-telegram"

apt-get update
apt-get install -y python3 python3-venv

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${DATA_DIR}"

rm -rf "${APP_DIR}/src" "${APP_DIR}/deploy"
cp -a "${SOURCE_DIR}/src" "${APP_DIR}/src"
cp -a "${SOURCE_DIR}/deploy" "${APP_DIR}/deploy"
install -o root -g root -m 0644 "${SOURCE_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${CONFIG_DIR}/eva-telegram.env" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 \
    "${SOURCE_DIR}/deploy/eva-telegram.env.example" \
    "${CONFIG_DIR}/eva-telegram.env"
fi

install -o root -g root -m 0644 \
  "${SOURCE_DIR}/deploy/eva-telegram.service" \
  "/etc/systemd/system/eva-telegram.service"

systemctl daemon-reload
echo
echo "Instalación terminada. Ahora configura los secretos con:"
echo "  sudo nano ${CONFIG_DIR}/eva-telegram.env"
echo "Después inicia el servicio con:"
echo "  sudo systemctl enable --now eva-telegram"
