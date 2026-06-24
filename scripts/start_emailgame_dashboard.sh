#!/usr/bin/env bash
set -euo pipefail

cd /home/ubuntu/hackathons/theemailgame
mkdir -p agent_logs

if ! tmux has-session -t emailgame-dashboard 2>/dev/null; then
  tmux new-session -d -s emailgame-dashboard './.venv/bin/python scripts/emailgame_dashboard.py --host 127.0.0.1 --port 8787'
fi

if command -v cloudflared >/dev/null 2>&1; then
  tmux kill-session -t emailgame-dashboard-tunnel 2>/dev/null || true
  tmux new-session -d -s emailgame-dashboard-tunnel 'cloudflared tunnel --url http://127.0.0.1:8787 2>&1 | tee agent_logs/emailgame-dashboard-tunnel.log'
fi

