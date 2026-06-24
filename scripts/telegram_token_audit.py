#!/usr/bin/env python3
"""Safe Telegram token routing audit for Email Game and Codex bots.

The script reads the checked-in and local env files, verifies bot ownership
with Telegram ``getMe``, checks webhook state, and inspects the bridge/
watchdog launch files for the expected env wiring. It never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_REPORT_PATH = PROJECT_ROOT / "agent_logs" / "telegram_token_audit.json"
ENV_LOCAL_PATH = Path("/home/ubuntu/hackathons/theemailgame/.env.local")
CODEX_TELEGRAM_ENV_PATH = Path("/home/ubuntu/.codex-telegram.env")
CODEX_WATCHDOG_ENV_PATH = Path("/home/ubuntu/.codex-watchdog.env")
BRIDGE_START_PATH = Path("/home/ubuntu/bin/codex-bridge-start")
WATCHDOG_START_PATH = Path("/home/ubuntu/bin/codex-watchdog-start")
BRIDGE_LOG_PATH = Path("/home/ubuntu/codex-logs/codex-telegram-bridge.log")
WATCHDOG_LOG_PATH = Path("/home/ubuntu/codex-logs/codex-watchdog.log")
MONITOR_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "monitor_emailgame_telegram.py"
BOT_TO_BOT_REPORT_PATH = PROJECT_ROOT / "agent_logs" / "emailgame-bot-to-bot-test.json"
QA_LAST_SEND_PATH = PROJECT_ROOT / "dashboard_qa" / "last_send_result.json"
TELEGRAM_API_BASE = "https://api.telegram.org/bot"
DEFAULT_TESTER_USERNAME = "EmailGameTesterBot"
DEFAULT_MAIN_USERNAME = "EmailGameBot"

TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
TOKEN_KV_RE = re.compile(r"(?i)\b(token|bearer)\s*[:=]\s*[A-Za-z0-9._-]{8,}")
URLSAFE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
BRIDGE_ROUTING_LABEL_RE = re.compile(r"routing_label=([A-Za-z0-9_]+)")
BRIDGE_ROUTING_BOT_RE = re.compile(r"routing_bot=(@[A-Za-z0-9_]+)")
BRIDGE_BOT_USERNAME_RE = re.compile(r"Bot username:\s*([A-Za-z0-9_]+)")


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        values[key] = value
    return values


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        text = TOKEN_RE.sub("[telegram token redacted]", value)
        text = TOKEN_KV_RE.sub("[redacted]", text)
        text = URLSAFE_TOKEN_RE.sub("[redacted-token]", text)
        return text
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    return value


def _mask_chat_id(value: str) -> str:
    text = str(value).strip()
    if not text:
        return "missing"
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:3]}…{text[-3:]}"


def _all_equal(values: Iterable[str]) -> bool:
    filtered = [value for value in values if value]
    return bool(filtered) and len(set(filtered)) == 1


def _bridge_listener_state(log_text: str) -> dict[str, str]:
    labels = BRIDGE_ROUTING_LABEL_RE.findall(log_text or "")
    bots = BRIDGE_ROUTING_BOT_RE.findall(log_text or "")
    usernames = BRIDGE_BOT_USERNAME_RE.findall(log_text or "")
    return {
        "label": labels[-1] if labels else "",
        "bot": bots[-1] if bots else "",
        "username": usernames[-1] if usernames else "",
    }


def _telegram_request(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    data = urlencode(payload).encode("utf-8")
    request = Request(
        f"{TELEGRAM_API_BASE}{token}/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _get_me(token: str) -> dict[str, Any]:
    if not token:
        return {}
    try:
        response = _telegram_request(token, "getMe")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}"}
    return response.get("result") if isinstance(response.get("result"), dict) else {}


def _get_webhook_info(token: str) -> dict[str, Any]:
    if not token:
        return {}
    try:
        response = _telegram_request(token, "getWebhookInfo")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}"}
    return response.get("result") if isinstance(response.get("result"), dict) else {}


def _tmux_has_session(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _file_mentions(path: Path, *needles: str) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return all(needle in text for needle in needles)


def _file_contains(path: Path, *needles: str) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return any(needle in text for needle in needles)


def _bot_entry(
    *,
    name: str,
    env: dict[str, str],
    token_keys: tuple[str, ...],
    expected_username: str | None,
) -> dict[str, Any]:
    token_key = next((key for key in token_keys if env.get(key, "").strip()), "")
    token = env.get(token_key, "").strip() if token_key else ""
    bot = _get_me(token)
    webhook = _get_webhook_info(token)
    actual_username = str(bot.get("username") or "").lstrip("@")
    expected = str(expected_username or "").lstrip("@")
    return {
        "name": name,
        "token_key": token_key,
        "token_present": bool(token),
        "actual_username": actual_username or "unknown",
        "expected_username": expected or "",
        "ownership_match": None if not expected else bool(actual_username and actual_username.lower() == expected.lower()),
        "webhook_url": str(webhook.get("url") or ""),
        "webhook_active": bool(str(webhook.get("url") or "").strip()),
        "webhook_pending_update_count": int(webhook.get("pending_update_count") or 0) if str(webhook.get("pending_update_count") or "").isdigit() else webhook.get("pending_update_count", 0),
        "webhook_last_error_message": str(webhook.get("last_error_message") or ""),
        "webhook_error_count": int(webhook.get("last_error_date") or 0) if str(webhook.get("last_error_date") or "").isdigit() else webhook.get("last_error_date", 0),
        "get_me_error": str(bot.get("error") or ""),
    }


def _safe_summary(report: dict[str, Any]) -> list[str]:
    def ownership_text(name: str) -> str:
        entry = report["bots"][name]
        if not entry.get("expected_username"):
            return "n/a"
        return "yes" if entry.get("ownership_match") else "no"

    lines = [
        f"report path: {AUDIT_REPORT_PATH.relative_to(PROJECT_ROOT)}",
        f"tester owner match: {ownership_text('tester')}",
        f"main owner match: {ownership_text('main')}",
        f"bridge owner match: {ownership_text('bridge')}",
        f"watchdog owner match: {ownership_text('watchdog')}",
        f"report chat match: {'yes' if report['chat_matches']['report_chat'] else 'no'}",
        f"webhook conflict: {'yes' if report['webhook_conflict'] else 'no'}",
        f"polling conflict: {'yes' if report['polling_conflict'] else 'no'}",
        f"bridge listener route: {report['bridge_listener'].get('label') or 'unknown'}",
        f"bridge listener bot: {report['bridge_listener'].get('username') or 'unknown'}",
        f"bridge restart required: {'yes' if report['bridge_restart_required'] else 'no'}",
        f"watchdog restart required: {'yes' if report['watchdog_restart_required'] else 'no'}",
        f"handler active: {'yes' if report['handler_active'] else 'no'}",
        f"tester_status added: {'yes' if report['tester_status_added'] else 'no'}",
        f"summary sent: {'yes' if report['dashboard_qa'].get('summary_sent') else 'no'}",
        f"screenshots sent: {report['dashboard_qa'].get('screenshots_sent', 0)}",
        f"proof file: {report['dashboard_qa'].get('proof_file') or 'missing'}",
    ]
    return lines


def build_report() -> dict[str, Any]:
    env_local = _load_env_file(ENV_LOCAL_PATH)
    codex_telegram = _load_env_file(CODEX_TELEGRAM_ENV_PATH)
    codex_watchdog = _load_env_file(CODEX_WATCHDOG_ENV_PATH)

    tester_expected = env_local.get("EMAIL_GAME_TESTER_BOT_USERNAME") or DEFAULT_TESTER_USERNAME
    main_expected = (
        env_local.get("EMAIL_GAME_TARGET_BOT_USERNAME")
        or codex_telegram.get("EMAIL_GAME_TARGET_BOT_USERNAME")
        or DEFAULT_MAIN_USERNAME
    )
    bridge_expected = (
        codex_telegram.get("EMAIL_GAME_TESTER_BOT_USERNAME")
        or env_local.get("EMAIL_GAME_TESTER_BOT_USERNAME")
        or DEFAULT_TESTER_USERNAME
    )
    watchdog_expected = (
        codex_watchdog.get("CODEX_WATCH_TELEGRAM_BOT_USERNAME")
        or codex_watchdog.get("CODEX_BRIDGE_TELEGRAM_BOT_USERNAME")
        or DEFAULT_MAIN_USERNAME
    )

    bots = {
        "tester": _bot_entry(
            name="tester",
            env=env_local,
            token_keys=("EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN",),
            expected_username=tester_expected,
        ),
        "main": _bot_entry(
            name="main",
            env=env_local | codex_telegram,
            token_keys=("EMAIL_GAME_TELEGRAM_BOT_TOKEN",),
            expected_username=main_expected,
        ),
        "bridge": _bot_entry(
            name="bridge",
            env=codex_telegram,
            token_keys=("CODEX_BRIDGE_TELEGRAM_BOT_TOKEN", "CODEX_OPS_TELEGRAM_BOT_TOKEN"),
            expected_username=bridge_expected,
        ),
        "watchdog": _bot_entry(
            name="watchdog",
            env=codex_watchdog,
            token_keys=("CODEX_WATCH_TELEGRAM_BOT_TOKEN", "CODEX_OPS_TELEGRAM_BOT_TOKEN"),
            expected_username=watchdog_expected,
        ),
    }

    report_chat_ids = {
        "env_local": env_local.get("EMAIL_GAME_TEST_REPORT_CHAT_ID", "").strip(),
        "codex_telegram": codex_telegram.get("CODEX_BRIDGE_TELEGRAM_CHAT_ID", "").strip(),
        "codex_watchdog": codex_watchdog.get("CODEX_WATCH_TELEGRAM_CHAT_ID", "").strip(),
    }
    report_chat_match = _all_equal(report_chat_ids.values())

    webhook_conflict = any(bots[name]["webhook_active"] for name in ("tester", "main", "bridge", "watchdog"))

    bridge_start_ok = _file_mentions(
        BRIDGE_START_PATH,
        'ENV_FILE="/home/ubuntu/.codex-telegram.env"',
        'source "$ENV_FILE"',
        "CODEX_BRIDGE_TELEGRAM_BOT_TOKEN",
        "CODEX_BRIDGE_TELEGRAM_CHAT_ID",
    )
    watchdog_start_ok = _file_mentions(
        WATCHDOG_START_PATH,
        'ENV_FILE="/home/ubuntu/.codex-telegram.env"',
        'source "$ENV_FILE"',
        "CODEX_WATCH_TELEGRAM_BOT_TOKEN",
        "CODEX_WATCH_TELEGRAM_CHAT_ID",
    )
    bridge_runtime = _tmux_has_session("telegram-bridge-daemon")
    watchdog_runtime = _tmux_has_session("codex-watchdog")
    bridge_log = BRIDGE_LOG_PATH.read_text(encoding="utf-8", errors="ignore") if BRIDGE_LOG_PATH.exists() else ""
    watchdog_log = WATCHDOG_LOG_PATH.read_text(encoding="utf-8", errors="ignore") if WATCHDOG_LOG_PATH.exists() else ""
    bridge_listener = _bridge_listener_state(bridge_log)
    bridge_loaded_env = bridge_start_ok and bridge_runtime and _file_contains(
        BRIDGE_LOG_PATH,
        "telegram config source=",
        "Bot username:",
        "Authorized chat:",
    )
    watchdog_loaded_env = watchdog_start_ok and watchdog_runtime and _file_contains(
        WATCHDOG_LOG_PATH,
        "telegram config source=",
        "Bot username:",
        "Authorized chat:",
    )

    handler_active = _file_mentions(
        MONITOR_SCRIPT_PATH,
        '"/dashboard_qa_report"',
        "def _dashboard_qa_report_text",
    )
    tester_status_added = _file_mentions(MONITOR_SCRIPT_PATH, '"/tester_status"')

    bot_to_bot_report = {}
    if BOT_TO_BOT_REPORT_PATH.exists():
        try:
            bot_to_bot_report = json.loads(BOT_TO_BOT_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            bot_to_bot_report = {}

    dashboard_qa = {}
    if QA_LAST_SEND_PATH.exists():
        try:
            dashboard_qa = json.loads(QA_LAST_SEND_PATH.read_text(encoding="utf-8"))
        except Exception:
            dashboard_qa = {}
    dashboard_qa = _redact(dashboard_qa) if dashboard_qa else {}
    dashboard_qa_report = {
        "summary_sent": bool(dashboard_qa.get("summary_sent")),
        "screenshots_found": int(dashboard_qa.get("screenshots_found") or 0),
        "screenshots_sent": int(dashboard_qa.get("screenshots_sent") or 0),
        "fallback_documents_sent": int(dashboard_qa.get("fallback_documents_sent") or 0),
        "telegram_message_ids_received": bool(dashboard_qa.get("telegram_message_ids_received")),
        "proof_file": str(QA_LAST_SEND_PATH.relative_to(PROJECT_ROOT)) if QA_LAST_SEND_PATH.exists() else "",
    }

    polling_conflict = False
    polling_notes = []
    if bot_to_bot_report:
        polling_conflict = bool(bot_to_bot_report.get("conflict_409_avoided") is False)
        if bot_to_bot_report.get("errors"):
            polling_notes = [str(item) for item in bot_to_bot_report.get("errors", [])]

    bridge_restart_required = bool(not bridge_loaded_env or not bridge_runtime)
    watchdog_restart_required = bool(not watchdog_loaded_env or not watchdog_runtime)

    report = {
        "env_files": {
            "env_local": str(ENV_LOCAL_PATH),
            "codex_telegram": str(CODEX_TELEGRAM_ENV_PATH),
            "codex_watchdog": str(CODEX_WATCHDOG_ENV_PATH),
        },
        "bots": bots,
        "chat_ids": {key: _mask_chat_id(value) for key, value in report_chat_ids.items()},
        "chat_matches": {
            "report_chat": report_chat_match,
            "bridge_chat_matches_report_chat": bool(report_chat_ids["env_local"] and report_chat_ids["env_local"] == report_chat_ids["codex_telegram"]),
            "watchdog_chat_matches_report_chat": bool(report_chat_ids["env_local"] and report_chat_ids["env_local"] == report_chat_ids["codex_watchdog"]),
        },
        "webhook_conflict": webhook_conflict,
        "polling_conflict": polling_conflict,
        "polling_notes": polling_notes,
        "bridge_runtime": bridge_runtime,
        "watchdog_runtime": watchdog_runtime,
        "bridge_listener": bridge_listener,
        "bridge_loaded_env": bridge_loaded_env,
        "watchdog_loaded_env": watchdog_loaded_env,
        "bridge_restart_required": bridge_restart_required,
        "watchdog_restart_required": watchdog_restart_required,
        "handler_active": handler_active,
        "tester_status_added": tester_status_added,
        "bot_to_bot_report": _redact(bot_to_bot_report),
        "dashboard_qa": dashboard_qa_report,
        "dashboard_qa_report": _redact(dashboard_qa),
        "summary_lines": [],
    }
    report["summary_lines"] = _safe_summary(report)
    return report


def _write_report(report: dict[str, Any]) -> None:
    AUDIT_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUDIT_REPORT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_redact(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(AUDIT_REPORT_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Telegram token routing and safe command wiring.")
    parser.add_argument("--json", action="store_true", help="Print the full redacted JSON report.")
    args = parser.parse_args()

    report = build_report()
    _write_report(report)

    for line in report["summary_lines"]:
        print(line)

    if args.json:
        print(json.dumps(_redact(report), indent=2, sort_keys=True))

    critical_ok = (
        report["bots"]["tester"]["ownership_match"]
        and report["bots"]["main"]["ownership_match"]
        and report["chat_matches"]["report_chat"]
        and not report["webhook_conflict"]
        and not report["polling_conflict"]
        and report["handler_active"]
    )
    return 0 if critical_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
