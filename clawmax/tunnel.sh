#!/bin/sh
# Cloudflare Tunnel for the ClawMax endpoint. Exposes ONLY Violet's endpoint server (127.0.0.1:8766),
# which serves nothing but POST /agent/message. Nothing else on this Mac is reachable.
#
#   clawmax/tunnel.sh start     quick tunnel (no account; new https://*.trycloudflare.com URL each start)
#   clawmax/tunnel.sh named     named tunnel "violet-clawmax" (stable hostname; needs clawmax/cloudflared.yml)
#   clawmax/tunnel.sh stop      stop whichever is running
#   clawmax/tunnel.sh url       print the current quick-tunnel URL
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/.tunnel"
mkdir -p "$STATE"
CF="$(command -v cloudflared || echo /opt/homebrew/opt/cloudflared/bin/cloudflared)"

case "$1" in
  start)
    "$0" stop >/dev/null 2>&1
    nohup "$CF" tunnel --no-autoupdate --url http://127.0.0.1:8766 > "$STATE/cloudflared.log" 2>&1 &
    echo $! > "$STATE/pid"
    for i in $(seq 1 30); do
      URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$STATE/cloudflared.log" | head -1)
      [ -n "$URL" ] && break
      sleep 1
    done
    [ -n "$URL" ] && { echo "$URL" > "$STATE/url"; echo "Tunnel up: $URL/agent/message"; } || { echo "No URL yet; see $STATE/cloudflared.log"; exit 1; }
    ;;
  named)
    "$0" stop >/dev/null 2>&1
    [ -f "$HERE/cloudflared.yml" ] || { echo "Create $HERE/cloudflared.yml first (see clawmax/README.md)"; exit 1; }
    nohup "$CF" tunnel --no-autoupdate --config "$HERE/cloudflared.yml" run > "$STATE/cloudflared.log" 2>&1 &
    echo $! > "$STATE/pid"
    echo "Named tunnel started (pid $(cat "$STATE/pid")); log: $STATE/cloudflared.log"
    ;;
  stop)
    if [ -f "$STATE/pid" ] && kill "$(cat "$STATE/pid")" 2>/dev/null; then echo "Tunnel stopped"; else echo "No tunnel running"; fi
    rm -f "$STATE/pid" "$STATE/url"
    ;;
  url)
    cat "$STATE/url" 2>/dev/null || echo "No quick tunnel running"
    ;;
  *)
    echo "usage: $0 start|named|stop|url"; exit 2
    ;;
esac
