#!/usr/bin/env python3
"""Register the Email Game Telegram bot command menu.

Uses EMAIL_GAME_TELEGRAM_BOT_TOKEN only. The token is never printed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 20

COMMANDS: List[Dict[str, str]] = [
    {"command": "help", "description": "show command menu"},
    {"command": "status", "description": "current agent state"},
    {"command": "logs", "description": "latest match summary"},
    {"command": "match", "description": "latest match only"},
    {"command": "why", "description": "explain recent reminders"},
    {"command": "reminders", "description": "explain recent reminders"},
    {"command": "tail", "description": "raw redacted log tail"},
    {"command": "leaderboard", "description": "current ranking"},
    {"command": "rank", "description": "my rank and gaps"},
    {"command": "participants", "description": "leaderboard visibility"},
    {"command": "readiness", "description": "competition readiness report"},
    {"command": "budget", "description": "LLM budget and remaining estimate"},
    {"command": "usage", "description": "recent LLM call usage"},
    {"command": "coach", "description": "performance analysis"},
    {"command": "recommend", "description": "next Codex goal"},
    {"command": "reviewmatch", "description": "latest match diagnosis"},
    {"command": "metrics", "description": "numeric performance summary"},
    {"command": "version", "description": "branch and commit"},
    {"command": "preflight", "description": "safe checks"},
    {"command": "startagent", "description": "start agent if idle"},
    {"command": "restartagent", "description": "safe restart"},
    {"command": "stopagent", "description": "stop agent only if safe"},
    {"command": "reconnectlog", "description": "reconnect tmux log pipe"},
]


def _token() -> str:
    load_dotenv(PROJECT_ROOT / ".env.local")
    token = os.getenv("EMAIL_GAME_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("EMAIL_GAME_TELEGRAM_BOT_TOKEN is not configured.")
    return token


def _telegram(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    params = params or {}
    payload = urlencode(params).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{_token()}/{method}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8", "replace")
    data = json.loads(body)
    if not data.get("ok"):
        description = str(data.get("description") or "Telegram API call failed")
        raise SystemExit(f"{method} failed: {description}")
    return data


def _command_summary(commands: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"/{str(item.get('command', '')).strip()} - {str(item.get('description', '')).strip()}"
        for item in commands
    )


def main() -> int:
    bot = _telegram("getMe").get("result") or {}
    username = str(bot.get("username") or "unknown")

    _telegram("setMyCommands", {"commands": json.dumps(COMMANDS, separators=(",", ":"))})

    verified = _telegram("getMyCommands").get("result")
    if not isinstance(verified, list):
        raise SystemExit("getMyCommands returned a malformed command list.")

    expected = [(item["command"], item["description"]) for item in COMMANDS]
    actual = [
        (str(item.get("command") or ""), str(item.get("description") or ""))
        for item in verified
    ]
    missing = [command for command in expected if command not in actual]
    extra = [command for command in actual if command not in expected]

    print(f"Bot username: @{username}")
    print(f"Commands registered: {len(verified)}")
    print(_command_summary(verified))
    print(f"Verification passed: {not missing and not extra}")
    if missing:
        print("Missing commands:", ", ".join(command for command, _ in missing))
    if extra:
        print("Extra commands:", ", ".join(command for command, _ in extra))
    return 0 if not missing and not extra else 1


if __name__ == "__main__":
    raise SystemExit(main())
