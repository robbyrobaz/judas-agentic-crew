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
cp "${SCRIPT_DIR}/judas-operator.service" "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-operator.timer"   "${SYSTEMD_USER_DIR}/"
# Phase 10 specialists — replace the old judas-research.timer monolith.
cp "${SCRIPT_DIR}/judas-researcher.service" "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-researcher.timer"   "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-trader.service"     "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-trader.timer"       "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-registrar.service"  "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-registrar.timer"    "${SYSTEMD_USER_DIR}/"

# Retire legacy research unit if previously installed.
if [ -f "${SYSTEMD_USER_DIR}/judas-research.timer" ]; then
  echo "Disabling legacy judas-research.timer ..."
  systemctl --user disable --now judas-research.timer 2>/dev/null || true
  rm -f "${SYSTEMD_USER_DIR}/judas-research.service" \
        "${SYSTEMD_USER_DIR}/judas-research.timer"
fi

echo "Reloading systemd user daemon ..."
systemctl --user daemon-reload

echo "Importing workshop seed portfolio ..."
/home/rob/judas-agentic-crew/.venv/bin/python /home/rob/judas-agentic-crew/scripts/import_workshop_seed.py || true

if command -v npm >/dev/null 2>&1 && [ -f "/home/rob/judas-agentic-crew/dashboard/package.json" ]; then
  echo "Installing dashboard dependencies and building frontend ..."
  cd /home/rob/judas-agentic-crew/dashboard
  npm ci
  npm run build
  cd /home/rob/judas-agentic-crew
else
  echo "Skipping dashboard build: npm not found."
fi

echo "Enabling judas-crew.timer ..."
systemctl --user enable judas-crew.timer

echo "Starting judas-crew.timer ..."
systemctl --user start judas-crew.timer

echo "Enabling judas-dashboard.service ..."
systemctl --user enable judas-dashboard.service

echo "Starting judas-dashboard.service ..."
systemctl --user restart judas-dashboard.service

echo "Enabling judas-operator.timer ..."
systemctl --user enable judas-operator.timer

echo "Starting judas-operator.timer ..."
systemctl --user start judas-operator.timer

echo "Enabling specialist timers (Phase 10) ..."
for unit in judas-researcher.timer judas-trader.timer judas-registrar.timer; do
  systemctl --user enable "${unit}"
  systemctl --user start  "${unit}"
done

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
echo "Operator timer status:"
systemctl --user status judas-operator.timer --no-pager || true

echo ""
echo "Specialist timers:"
for unit in judas-researcher.timer judas-trader.timer judas-registrar.timer; do
  systemctl --user status "${unit}" --no-pager || true
  echo ""
done
