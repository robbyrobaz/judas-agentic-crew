#!/usr/bin/env bash
# Install judas services to the user systemd directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

echo "Installing judas-crew systemd files to ${SYSTEMD_USER_DIR} ..."
mkdir -p "${SYSTEMD_USER_DIR}"

cp "${SCRIPT_DIR}/judas-crew.service" "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-crew.timer"   "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-dashboard.service" "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-research.service" "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-research.timer"   "${SYSTEMD_USER_DIR}/"

echo "Reloading systemd user daemon ..."
systemctl --user daemon-reload

echo "Importing workshop seed portfolio ..."
/home/rob/judas-agentic-crew/.venv/bin/python /home/rob/judas-agentic-crew/scripts/import_workshop_seed.py || true

echo "Enabling judas-crew.timer ..."
systemctl --user enable judas-crew.timer

echo "Starting judas-crew.timer ..."
systemctl --user start judas-crew.timer

echo "Enabling judas-dashboard.service ..."
systemctl --user enable judas-dashboard.service

echo "Starting judas-dashboard.service ..."
systemctl --user restart judas-dashboard.service

echo "Enabling judas-research.timer ..."
systemctl --user enable judas-research.timer

echo "Starting judas-research.timer ..."
systemctl --user start judas-research.timer

echo ""
echo "Done. Timer status:"
systemctl --user status judas-crew.timer --no-pager || true

echo ""
echo "Next fire times:"
systemctl --user list-timers judas-crew.timer --no-pager || true

echo ""
echo "Dashboard status:"
systemctl --user status judas-dashboard.service --no-pager || true

echo ""
echo "Research timer status:"
systemctl --user status judas-research.timer --no-pager || true
