#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -f .env.local ]]; then
  echo "Missing .env.local" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv/bin/python" >&2
  exit 1
fi

set -a
. ./.env.local
set +a

if [[ "${EMAIL_GAME_AGENT_NAME:-}" != "letlhogonolo_fanampe" ]]; then
  echo "EMAIL_GAME_AGENT_NAME must be exactly letlhogonolo_fanampe" >&2
  exit 1
fi

if [[ -z "${EMAIL_GAME_SERVER:-}" ]]; then
  echo "EMAIL_GAME_SERVER must be set" >&2
  exit 1
fi

./.venv/bin/python -m py_compile my_agent.py

if [[ "${CHECK_EMAIL_GAME_KEY:-0}" == "1" || "${1:-}" == "--check-key" ]]; then
  ./.venv/bin/python scripts/check_openai_key.py
fi
