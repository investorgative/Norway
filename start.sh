#!/usr/bin/env bash
# Starts NORWAY: paper trader (tmux "Norway"), dashboard on :8759 ("NorwayWeb"),
# Cloudflare quick tunnel ("NorwayCloud"). Every process restarts itself if it crashes.
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || { echo "venv missing - run ./setup.sh first"; exit 1; }
PORT=$("$PY" -c "import norway_core as c;print(c.load_config()['web'].get('port',8759))")
mkdir -p "$DIR/data/logs"
if ! tmux has-session -t Norway 2>/dev/null; then
  tmux new-session -d -s Norway "cd '$DIR' && while true; do '$PY' norway_trader.py; echo 'trader exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session Norway      (paper trader)"
else echo "tmux session Norway already running"; fi
if ! tmux has-session -t NorwayWeb 2>/dev/null; then
  tmux new-session -d -s NorwayWeb "cd '$DIR' && while true; do '$PY' norway_web.py; echo 'web exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session NorwayWeb   (dashboard http://localhost:$PORT)"
else echo "tmux session NorwayWeb already running"; fi
if command -v cloudflared >/dev/null 2>&1; then
  if ! tmux has-session -t NorwayCloud 2>/dev/null; then
    : > "$DIR/data/logs/cloudflared.log"
    tmux new-session -d -s NorwayCloud "cd '$DIR' && while true; do cloudflared tunnel --no-autoupdate --url http://localhost:$PORT 2>&1 | tee -a data/logs/cloudflared.log; echo 'tunnel exited - restarting in 10s'; sleep 10; done"
    echo "started tmux session NorwayCloud (quick tunnel - URL appears in a few seconds)"
    sleep 8
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$DIR/data/logs/cloudflared.log" | tail -1)
    [ -n "$URL" ] && echo "public URL: $URL" || echo "public URL: run 'tmux attach -t NorwayCloud' or click the PUBLIC chip in the dashboard header"
  else echo "tmux session NorwayCloud already running"; fi
else
  echo "cloudflared not installed - tunnel not started (dashboard still works on the LAN)"
fi
