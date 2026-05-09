#!/usr/bin/env bash
# Install judas-crew systemd user service and timer.
# Does NOT start the service — just installs and enables the timer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

echo "Installing judas-crew systemd files to ${SYSTEMD_USER_DIR} ..."
mkdir -p "${SYSTEMD_USER_DIR}"

cp "${SCRIPT_DIR}/judas-crew.service" "${SYSTEMD_USER_DIR}/"
cp "${SCRIPT_DIR}/judas-crew.timer"   "${SYSTEMD_USER_DIR}/"

echo "Reloading systemd user daemon ..."
systemctl --user daemon-reload

echo "Enabling judas-crew.timer ..."
systemctl --user enable judas-crew.timer

echo "Starting judas-crew.timer ..."
systemctl --user start judas-crew.timer

echo ""
echo "Done. Timer status:"
systemctl --user status judas-crew.timer --no-pager || true

echo ""
echo "Next fire times:"
systemctl --user list-timers judas-crew.timer --no-pager || true
