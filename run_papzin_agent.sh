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

exec ./.venv/bin/python scripts/run_custom_agent.py "$EMAIL_GAME_AGENT_NAME" --module my_agent.py --server "$EMAIL_GAME_SERVER"
