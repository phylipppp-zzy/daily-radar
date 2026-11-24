#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "${UNIT_DST}"
(cd "${REPO_DIR}" && python3 -m daily_radar --repo-root "${REPO_DIR}" init-config)

install -m 0644 "${SCRIPT_DIR}/daily-radar.service" "${UNIT_DST}/daily-radar.service"
install -m 0644 "${SCRIPT_DIR}/daily-radar.timer" "${UNIT_DST}/daily-radar.timer"

systemctl --user daemon-reload

if [[ "${1:-}" == "--enable" ]]; then
  systemctl --user enable --now daily-radar.timer
  systemctl --user list-timers daily-radar.timer --no-pager
else
  echo "Installed units to ${UNIT_DST}. Run '$0 --enable' after filling ~/.config/daily-radar/webhook.json and pushing the GitHub Pages repo."
fi
