#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_PATH="$ROOT_DIR/linux/zapret_linux.py"
UNIT_NAME="zapret-discord-youtube-linux.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"

command="${1:-status}"

case "$command" in
  start)
    shift || true
    if [[ $# -gt 0 ]]; then
      exec "$PYTHON_BIN" "$SCRIPT_PATH" install-systemd "$1" --enable-now
    fi
    exec "$PYTHON_BIN" "$SCRIPT_PATH" install-systemd --enable-now
    ;;
  restart)
    shift || true
    if [[ $# -gt 0 ]]; then
      exec "$PYTHON_BIN" "$SCRIPT_PATH" install-systemd "$1" --enable-now
    fi
    if [[ -f "$UNIT_PATH" ]]; then
      exec systemctl restart "$UNIT_NAME"
    fi
    exec "$PYTHON_BIN" "$SCRIPT_PATH" restart
    ;;
  stop)
    if [[ -f "$UNIT_PATH" ]]; then
      exec systemctl stop "$UNIT_NAME"
    fi
    exec "$PYTHON_BIN" "$SCRIPT_PATH" stop
    ;;
  status)
    if [[ -f "$UNIT_PATH" ]]; then
      systemctl --no-pager --full status "$UNIT_NAME" || true
      echo
    fi
    exec "$PYTHON_BIN" "$SCRIPT_PATH" status
    ;;
  remove|remove-systemd)
    exec "$PYTHON_BIN" "$SCRIPT_PATH" remove-systemd
    ;;
  *)
    exec "$PYTHON_BIN" "$SCRIPT_PATH" "$@"
    ;;
esac
