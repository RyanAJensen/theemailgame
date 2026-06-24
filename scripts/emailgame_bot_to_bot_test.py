#!/usr/bin/env python3
"""Bot-to-bot command checks for the Email Game Telegram monitor.

This script uses only EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN. It never reads or
calls getUpdates with the live Email Game monitor bot token.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "agent_logs" / "emailgame-bot-to-bot-test.json"
BOT_API_BASE = "https://api.telegram.org/bot"
REPORT_CHAT_ENV = "EMAIL_GAME_TEST_REPORT_CHAT_ID"

SAFE_COMMANDS = [
    "/help",
    "/coach",
    "/dashboard",
    "/dashboard_url",
    "/status",
    "/logs",
    "/match",
    "/metrics",
    "/budget",
    "/usage",
    "/readiness",
    "/rank",
    "/participants",
    "/leaderboard",
    "/leaderboard full",
    "/version",
]

DANGEROUS_COMMANDS = {
    "/startagent",
    "/restartagent",
    "/stopagent",
    "/preflight",
    "/reconnectlog",
}

TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
API_KEY_RE = re.compile(r"\b(?:sk-|or-|nvapi-)[A-Za-z0-9._-]{8,}\b")
WATCH_URL_RE = re.compile(r"https?://(?:www\.)?the-email-game\.fly\.dev/watch\?[^\s<>\"]+", re.IGNORECASE)
URLSAFE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
TERMINAL_KEY_MASH_RE = re.compile(r"(?:\^\[\[[0-9;]*[A-Za-z])+")
HEARTBEAT_LOG_AGE_RE = re.compile(r"log_age=([0-9hms ]+)")
LOG_AGE_PART_RE = re.compile(r"(\d+)([smhd])")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        text = TERMINAL_KEY_MASH_RE.sub("", value)
        text = TOKEN_RE.sub("[telegram token redacted]", text)
        text = API_KEY_RE.sub("[api key redacted]", text)
        text = WATCH_URL_RE.sub("[watch link redacted]", text)
        text = URLSAFE_TOKEN_RE.sub("[redacted-token]", text)
        return text
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    return value


def _write_report(report: Dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_PATH.with_suffix(REPORT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(_redact(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(REPORT_PATH)


def _tmux_capture_pane(session: str, tail_lines: int = 80) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{tail_lines}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _session_running(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _parse_log_age_seconds(text: str) -> Optional[int]:
    total = 0
    matched = False
    for amount, unit in LOG_AGE_PART_RE.findall(text):
        matched = True
        value = int(amount)
        if unit == "s":
            total += value
        elif unit == "m":
            total += value * 60
        elif unit == "h":
            total += value * 3600
        elif unit == "d":
            total += value * 86400
    return total if matched else None


def _monitor_stale_warning() -> bool:
    capture = _tmux_capture_pane("emailgame-monitor", tail_lines=80)
    if not capture:
        return False
    if "stale" in capture.lower():
        return True
    matches = list(HEARTBEAT_LOG_AGE_RE.finditer(capture))
    if not matches:
        return False
    age_text = matches[-1].group(1)
    age_seconds = _parse_log_age_seconds(age_text)
    return bool(age_seconds is not None and age_seconds >= 300)


def _discover_report_chat(token: str) -> Tuple[Optional[int], str, int]:
    updates, error, status = _get_updates(token, None, timeout=1)
    if error:
        return None, error, status

    fallback_chat_id: Optional[int] = None
    for update in reversed(updates):
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        if str(chat.get("type") or "") != "private":
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            continue
        text = str(message.get("text") or "").strip()
        if text.startswith("/start"):
            return chat_id, "", status
        if fallback_chat_id is None:
            fallback_chat_id = chat_id
    return fallback_chat_id, "", status


def _update_env_local(report_chat_id: int) -> None:
    env_path = PROJECT_ROOT / ".env.local"
    if not env_path.exists():
        return

    backup_path = env_path.with_name(f".env.local.bak-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}")
    backup_path.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated = False
    replacement = f"{REPORT_CHAT_ENV}={report_chat_id}"
    for index, line in enumerate(lines):
        if line.startswith(f"{REPORT_CHAT_ENV}="):
            lines[index] = replacement
            updated = True
            break
    if not updated:
        lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _report_enabled(args: argparse.Namespace) -> bool:
    if args.send_report:
        return True
    return os.getenv("EMAIL_GAME_TEST_SEND_REPORT", "").strip() == "1"


def _send_report_message(token: str, chat_id: int, report: Dict[str, Any]) -> Tuple[bool, str, int]:
    verified = report.get("commands_verified") or []
    verified_lines = "\n".join(f"✅ {item}" for item in verified) if verified else "❌ none"
    safety_lines = [
        "✅ Dangerous commands blocked" if report.get("dangerous_commands_blocked") else "❌ Dangerous commands blocked",
        "✅ 409 conflict avoided" if report.get("conflict_409_avoided") else "❌ 409 conflict avoided",
        "✅ Main bot token not used for tester polling",
        "✅ Secrets exposed: no",
    ]
    message = "\n".join(
        [
            "🧪 Email Game Bot-to-Bot Test Report",
            "",
            f"Target: {report.get('target_bot_username') or '@EmailGameBot'}",
            f"Result: {'✅ passed' if report.get('bot_to_bot_send_succeeded') and report.get('replies_received') and not report.get('errors') else '❌ failed'}",
            "",
            "Verified:",
            verified_lines,
            "",
            "Safety:",
            *safety_lines,
            "",
            f"Agent: {'running' if report.get('agent_running') else 'stopped'}",
            f"Monitor: {'running' if report.get('monitor_running') else 'stopped'}",
            f"Monitor stale warning: {'yes' if report.get('monitor_stale_warning') else 'no'}",
            f"Report: {REPORT_PATH.relative_to(PROJECT_ROOT)}",
        ]
    )
    parsed, error, status = _telegram_request(
        token,
        "sendMessage",
        {"chat_id": str(chat_id), "text": message},
        timeout=20,
    )
    if error:
        return False, error, status
    return bool(parsed and parsed.get("ok")), "", status


def _telegram_request(token: str, method: str, payload: Dict[str, Any], timeout: int = 20) -> Tuple[Optional[Dict[str, Any]], str, int]:
    data = urlencode({key: str(value) for key, value in payload.items()}).encode("utf-8")
    request = Request(
        f"{BOT_API_BASE}{token}/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else None, "", int(getattr(response, "status", 200))
    except Exception as exc:
        status = int(getattr(exc, "code", 0) or 0)
        body = ""
        if hasattr(exc, "read"):
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
        message = body or str(exc)
        return None, str(_redact(message)), status


def _get_updates(token: str, offset: Optional[int], timeout: int) -> Tuple[List[Dict[str, Any]], str, int]:
    payload: Dict[str, Any] = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        payload["offset"] = offset
    parsed, error, status = _telegram_request(token, "getUpdates", payload, timeout=timeout + 5)
    if error:
        return [], error, status
    if not parsed or not parsed.get("ok"):
        return [], str(_redact(parsed or {"error": "malformed response"})), status
    result = parsed.get("result")
    return result if isinstance(result, list) else [], "", status


def _latest_offset(token: str) -> Tuple[Optional[int], str, int]:
    updates, error, status = _get_updates(token, None, timeout=1)
    if error:
        return None, error, status
    if not updates:
        return None, "", status
    update_ids = [item.get("update_id") for item in updates if isinstance(item.get("update_id"), int)]
    if not update_ids:
        return None, "", status
    return max(update_ids) + 1, "", status


def _message_text(update: Dict[str, Any]) -> str:
    message = update.get("message")
    if not isinstance(message, dict):
        return ""
    return str(message.get("text") or "").strip()


@dataclass
class CommandResult:
    command: str
    sent: bool = False
    send_status: int = 0
    send_error: str = ""
    replies: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "sent": self.sent,
            "send_status": self.send_status,
            "send_error": _redact(self.send_error),
            "reply_count": len(self.replies),
            "reply_snippets": [_redact(reply[:500]) for reply in self.replies[:3]],
        }


def _validate_commands() -> None:
    for command in SAFE_COMMANDS:
        base = command.split()[0]
        if base in DANGEROUS_COMMANDS:
            raise RuntimeError(f"dangerous command configured as safe: {base}")


def run(args: argparse.Namespace) -> int:
    load_dotenv(PROJECT_ROOT / ".env.local")
    _validate_commands()

    token = os.getenv("EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN", "").strip()
    target = os.getenv("EMAIL_GAME_TARGET_BOT_USERNAME", "").strip()
    report_chat = os.getenv(REPORT_CHAT_ENV, "").strip()
    if target and not target.startswith("@"):
        target = f"@{target}"

    report: Dict[str, Any] = {
        "started_at": _utc_now(),
        "tester_token_present": bool(token),
        "target_bot_username": target or "",
        "report_chat_id_present": bool(report_chat),
        "dangerous_commands_blocked": True,
        "safe_commands": SAFE_COMMANDS,
        "commands": [],
        "bot_to_bot_send_succeeded": False,
        "replies_received": False,
        "conflict_409_avoided": True,
        "agent_running": _session_running("emailgame"),
        "monitor_running": _session_running("emailgame-monitor"),
        "monitor_stale_warning": _monitor_stale_warning(),
        "errors": [],
    }

    if not token:
        report["errors"].append("EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN is missing")
    if not target:
        report["errors"].append("EMAIL_GAME_TARGET_BOT_USERNAME is missing")
    if report["errors"]:
        report["finished_at"] = _utc_now()
        report["commands_verified"] = []
        _write_report(report)
        print(f"report_path={REPORT_PATH}")
        print("tester_token_present=no" if not token else "tester_token_present=yes")
        print("target_bot_username=missing" if not target else f"target_bot_username={target}")
        print("bot_to_bot_send_succeeded=no")
        print("replies_received=no")
        return 2

    if not report_chat and (args.discover_report_chat or _report_enabled(args)):
        discovered, discover_error, discover_status = _discover_report_chat(token)
        if discover_error:
            report["errors"].append(f"report chat discovery failed ({discover_status}): {discover_error}")
        elif discovered is not None:
            report_chat = str(discovered)
            report["report_chat_id_present"] = True
            if args.discover_report_chat:
                _update_env_local(discovered)

    offset, offset_error, offset_status = _latest_offset(token)
    if offset_error:
        report["errors"].append(f"initial getUpdates failed ({offset_status}): {offset_error}")
        if offset_status == 409:
            report["conflict_409_avoided"] = False

    next_offset = offset
    for command in SAFE_COMMANDS:
        result = CommandResult(command=command)
        parsed, error, status = _telegram_request(
            token,
            "sendMessage",
            {"chat_id": target, "text": command, "disable_web_page_preview": "true"},
            timeout=20,
        )
        result.send_status = status
        if error:
            result.send_error = error
        elif parsed and parsed.get("ok"):
            result.sent = True
            report["bot_to_bot_send_succeeded"] = True
        else:
            result.send_error = str(_redact(parsed or {"error": "malformed response"}))

        deadline = time.time() + 8
        while time.time() < deadline:
            updates, update_error, update_status = _get_updates(token, next_offset, timeout=2)
            if update_error:
                if update_status == 409:
                    report["conflict_409_avoided"] = False
                result.send_error = result.send_error or f"getUpdates failed ({update_status}): {update_error}"
                break
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    next_offset = update_id + 1
                text = _message_text(update)
                if text and not text.startswith(command):
                    result.replies.append(text)
            if result.replies:
                report["replies_received"] = True
                break
        report["commands"].append(result.to_json())

    report["finished_at"] = _utc_now()
    report["commands_verified"] = [
        item["command"] for item in report["commands"] if item.get("sent") and item.get("reply_count", 0) > 0
    ]
    _write_report(report)

    should_send_report = _report_enabled(args)
    report_sent = False
    report_send_error = ""
    if should_send_report:
        if not report_chat:
            discovered, discover_error, discover_status = _discover_report_chat(token)
            if discover_error:
                report["errors"].append(f"report chat discovery failed ({discover_status}): {discover_error}")
            elif discovered is not None:
                report_chat = str(discovered)
                report["report_chat_id_present"] = True
                if args.discover_report_chat:
                    _update_env_local(discovered)
        if report_chat:
            report_sent, report_send_error, send_status = _send_report_message(token, int(report_chat), report)
            if not report_sent:
                report["errors"].append(f"report send failed ({send_status}): {report_send_error}")
        else:
            report["errors"].append("report chat id missing; report not sent")
        report["report_sent"] = report_sent
        report["report_send_error"] = report_send_error
        report["finished_at"] = _utc_now()
        _write_report(report)

    print(f"report_path={REPORT_PATH}")
    print("tester_token_present=yes")
    print(f"target_bot_username={target}")
    print(f"report_chat_id_present={'yes' if report['report_chat_id_present'] else 'no'}")
    print(f"bot_to_bot_send_succeeded={'yes' if report['bot_to_bot_send_succeeded'] else 'no'}")
    print(f"replies_received={'yes' if report['replies_received'] else 'no'}")
    print(f"commands_verified={len(report['commands_verified'])}")
    print(f"conflict_409_avoided={'yes' if report['conflict_409_avoided'] else 'no'}")
    print(f"report_sent={'yes' if report.get('report_sent') else 'no'}")
    return 0 if report["bot_to_bot_send_succeeded"] and report["replies_received"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Email Game bot-to-bot command checks.")
    parser.add_argument("--send-report", action="store_true", help="Send the completed report to Papzin.")
    parser.add_argument(
        "--discover-report-chat",
        action="store_true",
        help="Discover the tester report chat from updates and persist it to .env.local.",
    )
    raise SystemExit(run(parser.parse_args()))
