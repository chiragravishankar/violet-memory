#!/bin/sh
# Run OpenClaw for Violet: Node 26, a private state/config folder, and only the environment it needs.
# Secrets come from Violet's .env: OPENCLAW_GATEWAY_TOKEN, and the bot token named by OC_BOT_TOKEN_VAR
# (TELEGRAM_TEST_BOT_TOKEN by default; TELEGRAM_BOT_TOKEN only at cutover). Nothing else from .env is passed.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
get() { grep -E "^$1=" "$ROOT/.env" | head -1 | cut -d= -f2-; }
VAR="${OC_BOT_TOKEN_VAR:-TELEGRAM_TEST_BOT_TOKEN}"
exec env -i \
  HOME="$HOME" \
  PATH="/opt/homebrew/opt/node/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  OPENCLAW_STATE_DIR="$HERE/state" \
  OPENCLAW_CONFIG_PATH="$HERE/state/openclaw.json" \
  OPENCLAW_GATEWAY_TOKEN="$(get OPENCLAW_GATEWAY_TOKEN)" \
  VIOLET_BOT_TOKEN="$(get "$VAR")" \
  VIOLET_BRIDGE_TOKEN="$(get VIOLET_BRIDGE_TOKEN)" \
  VIOLET_BRIDGE_URL="${VIOLET_BRIDGE_URL:-http://127.0.0.1:8765}" \
  OPENCLAW_SKIP_CRON=1 \
  "$HERE/runtime/node_modules/.bin/openclaw" "$@"
