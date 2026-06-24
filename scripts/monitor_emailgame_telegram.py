#!/usr/bin/env python3
"""Telegram monitor and control loop for the Email Game agent.

The monitor watches the live tmux log, emits Telegram alerts for notable state
changes, and accepts a small whitelisted command set from the authorized chat.
It never executes arbitrary shell input.
"""

from __future__ import annotations

import json
import os
import re
import sys
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

try:
    from emailgame_coach import EmailGameCoach
except ModuleNotFoundError:  # pragma: no cover - supports module-style imports in tests.
    from scripts.emailgame_coach import EmailGameCoach

try:
    from emailgame_budget import EmailGameBudget
except ModuleNotFoundError:  # pragma: no cover - supports module-style imports in tests.
    from scripts.emailgame_budget import EmailGameBudget

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - zoneinfo is available on modern Python, but keep fallback.
    ZoneInfo = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-live.log"
STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-monitor-state.json"
LEADERBOARD_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-leaderboard-state.json"
BUDGET_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-budget-state.json"
EVENTS_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-events.jsonl"
POLL_INTERVAL_SEC = 1.0
TELEGRAM_POLL_INTERVAL_SEC = 0.0
TELEGRAM_HTTP_TIMEOUT_SEC = 40
DEFAULT_LEADERBOARD_POLL_SECONDS = 180
MAX_SEEN_LINES = 2000
MAX_LOG_LINES = 40
MAX_TAIL_LINES = 25
MAX_STATUS_LINES = 20
MAX_TELEGRAM_CHUNK = 3500
STALE_LOG_THRESHOLD_SEC = 300
TMUX_SESSION_AGENT = "emailgame"
TMUX_SESSION_MONITOR = "emailgame-monitor"
TELEGRAM_PARSE_MODE = "HTML"
MAX_OBSERVED_LINES = 180
MAX_EVENT_READ_LINES = 3000
MAX_COMMAND_AUDIT = 40
HOUSE_BOT_IDS = {"house_bot_1", "house_bot_2", "house_bot_3"}
BOT_TO_BOT_SAFE_COMMANDS = {
    "/help",
    "/coach",
    "/dashboard",
    "/dashboard_url",
    "/dashboard_refresh",
    "/dashboard_qa_report",
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
    "/version",
}
DEFAULT_TESTER_BOT_USERNAME = "EmailGameTesterBot"
DASHBOARD_URL_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-dashboard-url.txt"
DASHBOARD_TUNNEL_LOG_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-dashboard-tunnel.log"
DASHBOARD_TUNNEL_SESSION = "emailgame-dashboard-tunnel"
PUBLIC_DASHBOARD_URL_RE = re.compile(r"https?://[^\s<>\"]*trycloudflare\.com[^\s<>\"]*", re.IGNORECASE)

OSC8_LINK_RE = re.compile(r"\x1b]8;;.*?\x1b\\")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_TERMINATOR_RE = re.compile(r"\x1b\\")
WATCH_URL_RE = re.compile(
    r"https?://(?:www\.)?the-email-game\.fly\.dev/watch\?[^\s<>\"]+",
    flags=re.IGNORECASE,
)
TOKEN_KV_RE = re.compile(r"(token=)[^\s&<>\"]+", flags=re.IGNORECASE)
SENSITIVE_KV_RE = re.compile(
    r"(\b(?:OPENAI_API_KEY|EMAIL_GAME_API_KEY|EMAIL_GAME_TELEGRAM_BOT_TOKEN|"
    r"TELEGRAM_BOT_TOKEN|TG_BOT_TOKEN|WATCH_URL_TOKEN|API_KEY)\b\s*=?\s*)[^\s<>\"]+",
    flags=re.IGNORECASE,
)
OPENAI_STYLE_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{6,}\b")
ELLIPSIZED_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{3,}\.\.\.[A-Za-z0-9._-]{2,}\b")
URLSAFE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
TERMINAL_KEY_MASH_RE = re.compile(r"(?:\^\[\[[0-9;]*[A-Za-z])+")
SEPARATOR_RE = re.compile(r"^[\s\-_=─━│┄┅┆┇┈┉┊┋╌╍╴╶╼╾]+$")
SAST_TZ = ZoneInfo("Africa/Johannesburg") if ZoneInfo is not None else timezone(timedelta(hours=2))
ET_TZ = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone(timedelta(hours=-4))
COMPETITION_START_ET = datetime(2026, 6, 27, 11, 0, tzinfo=ET_TZ)
COMPETITION_END_ET = datetime(2026, 6, 27, 17, 0, tzinfo=ET_TZ)

load_dotenv(PROJECT_ROOT / ".env.local")


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _redact(text: str) -> str:
    text = text.replace("\r", "")
    text = TERMINAL_KEY_MASH_RE.sub("", text)
    text = OSC8_LINK_RE.sub("", text)
    text = OSC_TERMINATOR_RE.sub("", text)
    text = ANSI_ESCAPE_RE.sub("", text)
    text = WATCH_URL_RE.sub("[watch link redacted]", text)
    text = TOKEN_KV_RE.sub(r"\1[redacted]", text)
    text = SENSITIVE_KV_RE.sub(r"\1[redacted]", text)
    text = ELLIPSIZED_TOKEN_RE.sub("[redacted]", text)
    text = OPENAI_STYLE_TOKEN_RE.sub("[redacted]", text)
    text = URLSAFE_TOKEN_RE.sub("[redacted-token]", text)
    text = re.sub(r"(Authorization:\s*Bearer\s+)[^\s<>\"]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    return text


def _clean_log_text(text: str) -> str:
    cleaned_lines: List[str] = []
    previous = ""
    for raw_line in text.splitlines():
        line = _redact(raw_line).strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            previous = ""
            continue
        if SEPARATOR_RE.match(line):
            continue
        if line == previous:
            continue
        cleaned_lines.append(line)
        previous = line
    while cleaned_lines and cleaned_lines[0] == "":
        cleaned_lines.pop(0)
    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()
    return "\n".join(cleaned_lines)


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _extract_public_dashboard_url(text: str) -> str:
    match = PUBLIC_DASHBOARD_URL_RE.search(text or "")
    return match.group(0).rstrip(".,);]}>\"'") if match else ""


def _build_protected_dashboard_url(base_url: str) -> str:
    token = _read_text_file(PROJECT_ROOT / "agent_logs" / "emailgame-dashboard-token.txt")
    if not base_url or not token:
        return ""
    return f"{base_url.rstrip('/')}/d/{token}/"


def _now_sast() -> datetime:
    return datetime.now(tz=SAST_TZ)


def _format_sast(dt: Optional[datetime] = None) -> str:
    return (dt or _now_sast()).astimezone(SAST_TZ).strftime("%H:%M SAST")


def _format_countdown(target: datetime, now: Optional[datetime] = None) -> str:
    current = now or datetime.now(tz=target.tzinfo)
    remaining = target - current.astimezone(target.tzinfo)
    if remaining.total_seconds() <= 0:
        return "started"
    total_seconds = int(remaining.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _score_delta_text(delta: Optional[int]) -> str:
    if delta is None:
        return "n/a"
    return f"{delta:+d}"


def _parse_sast(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SAST_TZ)
    return parsed.astimezone(SAST_TZ)


def _chunk_text(text: str, limit: int = MAX_TELEGRAM_CHUNK) -> List[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.splitlines():
        piece = line + "\n"
        if current and size + len(piece) > limit:
            chunks.append("".join(current).rstrip())
            current = []
            size = 0
        if len(piece) > limit:
            if current:
                chunks.append("".join(current).rstrip())
                current = []
                size = 0
            for start in range(0, len(line), limit):
                chunks.append(line[start : start + limit])
            continue
        current.append(piece)
        size += len(piece)

    if current:
        chunks.append("".join(current).rstrip())
    return [chunk for chunk in chunks if chunk]


def _run_command(args: List[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _tmux_has_session(name: str) -> bool:
    result = _run_command(["tmux", "has-session", "-t", name], timeout=5)
    return result.returncode == 0


def _tmux_send_keys(name: str, command: str) -> subprocess.CompletedProcess[str]:
    return _run_command(["tmux", "send-keys", "-t", name, command, "C-m"], timeout=10)


def _tmux_send_ctrl_c(name: str) -> subprocess.CompletedProcess[str]:
    return _run_command(["tmux", "send-keys", "-t", name, "C-c"], timeout=10)


def _tmux_start_agent_session() -> subprocess.CompletedProcess[str]:
    return _run_command(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            TMUX_SESSION_AGENT,
            "-c",
            str(PROJECT_ROOT),
            "bash",
            "-lc",
            "cd /home/ubuntu/hackathons/theemailgame && bash preflight_papzin_agent.sh --check-key && bash run_papzin_agent.sh",
        ],
        timeout=10,
    )


def _tmux_launch_agent_in_session() -> subprocess.CompletedProcess[str]:
    return _tmux_send_keys(
        TMUX_SESSION_AGENT,
        "cd /home/ubuntu/hackathons/theemailgame && bash preflight_papzin_agent.sh --check-key && bash run_papzin_agent.sh",
    )


def _tmux_pane_pid(name: str) -> Optional[int]:
    result = _run_command(["tmux", "display-message", "-p", "-t", name, "#{pane_pid}"], timeout=5)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _tmux_pane_id(name: str) -> str:
    result = _run_command(["tmux", "display-message", "-p", "-t", name, "#{pane_id}"], timeout=5)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _tmux_pane_pipe_connected(name: str) -> Optional[bool]:
    result = _run_command(["tmux", "display-message", "-p", "-t", name, "#{pane_pipe}"], timeout=5)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def _tmux_capture_pane(name: str, lines: int = 120) -> List[str]:
    result = _run_command(["tmux", "capture-pane", "-J", "-t", name, "-p"], timeout=5)
    if result.returncode != 0:
        return []
    cleaned = [_clean_log_text(line) for line in result.stdout.splitlines()]
    return [line for line in cleaned if line][-lines:]


def _tmux_reconnect_log_pipe(log_file: Path) -> Tuple[bool, str]:
    if not _tmux_has_session(TMUX_SESSION_AGENT):
        return False, f"tmux session {TMUX_SESSION_AGENT!r} is not running"
    clear = _run_command(["tmux", "pipe-pane", "-t", TMUX_SESSION_AGENT], timeout=5)
    if clear.returncode != 0:
        return False, (clear.stderr or clear.stdout or "failed to clear existing tmux pipe").strip()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    attach = _run_command(
        ["tmux", "pipe-pane", "-o", "-t", TMUX_SESSION_AGENT, f"cat >> {log_file}"],
        timeout=5,
    )
    if attach.returncode != 0:
        return False, (attach.stderr or attach.stdout or "failed to attach tmux pipe").strip()
    return True, ""


def _process_running(pid: Optional[int]) -> bool:
    if not pid:
        return False
    result = _run_command(["ps", "-p", str(pid), "-o", "pid="], timeout=5)
    return result.returncode == 0 and result.stdout.strip() == str(pid)


def _process_running_pattern(pattern: str) -> bool:
    result = _run_command(["pgrep", "-af", pattern], timeout=5)
    if result.returncode != 0:
        return False
    return any(line.strip() for line in result.stdout.splitlines())


def _read_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class Event:
    kind: str
    text: str


@dataclass(frozen=True)
class ObservedLine:
    ts: datetime
    text: str

    def to_json(self) -> Dict[str, str]:
        return {"ts": self.ts.isoformat(), "text": self.text}

    @classmethod
    def from_json(cls, raw: object) -> Optional["ObservedLine"]:
        if not isinstance(raw, dict):
            return None
        text = _redact(str(raw.get("text") or "")).strip()
        ts = _parse_sast(raw.get("ts"))
        if not text or ts is None:
            return None
        return cls(ts=ts, text=text)


@dataclass(frozen=True)
class StructuredEvent:
    ts: datetime
    type: str
    round: Optional[int]
    agent: str
    counterparty: str
    message: str
    model: str = ""
    tokens: Optional[Dict[str, int]] = None
    cost_usd: Optional[float] = None

    def to_json(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "ts": self.ts.isoformat(),
            "type": self.type,
            "round": self.round,
            "agent": self.agent,
            "counterparty": self.counterparty,
            "message": self.message,
        }
        if self.model:
            payload["model"] = self.model
        if self.type == "llm_call" or self.tokens is not None:
            payload["tokens"] = self.tokens
        if self.type == "llm_call" or self.cost_usd is not None:
            payload["cost_usd"] = self.cost_usd
        return payload

    @classmethod
    def from_json(cls, raw: object) -> Optional["StructuredEvent"]:
        if not isinstance(raw, dict):
            return None
        ts = _parse_sast(raw.get("ts"))
        event_type = str(raw.get("type") or "").strip()
        message = _clean_log_text(str(raw.get("message") or ""))
        if ts is None or not event_type or not message:
            return None
        round_value: Optional[int] = None
        raw_round = raw.get("round")
        try:
            if raw_round not in (None, ""):
                round_value = int(raw_round)
        except Exception:
            round_value = None
        tokens = raw.get("tokens")
        return cls(
            ts=ts,
            type=event_type,
            round=round_value,
            agent=_redact(str(raw.get("agent") or "")).strip(),
            counterparty=_redact(str(raw.get("counterparty") or "")).strip(),
            message=message,
            model=_redact(str(raw.get("model") or "")).strip(),
            tokens=tokens if isinstance(tokens, dict) else None,
            cost_usd=raw.get("cost_usd") if isinstance(raw.get("cost_usd"), (int, float)) else None,
        )


@dataclass
class RoundSummary:
    round_id: str
    started_at: datetime
    requested_from: List[str] = field(default_factory=list)
    request_targets: Optional[int] = None
    received_from: List[str] = field(default_factory=list)
    signed_from: List[str] = field(default_factory=list)
    submitted: Optional[bool] = None
    reminders: int = 0


@dataclass
class MatchSummary:
    started_at: datetime
    rounds: Dict[str, RoundSummary] = field(default_factory=dict)
    ended_at: Optional[datetime] = None
    ended: bool = False
    source_count: int = 0


@dataclass(frozen=True)
class LogStreamStatus:
    exists: bool
    age_seconds: Optional[float]
    stale: bool
    file_stale: bool = False
    pane_age_seconds: Optional[float] = None
    pane_observed: bool = False
    reconnected: bool = False
    reconnect_error: str = ""


@dataclass
class MonitorState:
    log_offset: int = 0
    telegram_offset: int = 0
    phase: str = "unknown"
    last_event: str = ""
    connected_sent_pid: Optional[int] = None
    connected_sent_at: str = ""
    observed_lines: List[Dict[str, str]] = field(default_factory=list)
    telegram_commands: List[Dict[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "MonitorState":
        raw = _read_json(path)
        observed_lines = []
        telegram_commands = []
        for item in raw.get("observed_lines", []) if isinstance(raw, dict) else []:
            if isinstance(item, dict):
                ts = str(item.get("ts") or "").strip()
                text = str(item.get("text") or "").strip()
                if ts and text:
                    observed_lines.append({"ts": ts, "text": text})
        for item in raw.get("telegram_commands", []) if isinstance(raw, dict) else []:
            if isinstance(item, dict):
                ts = str(item.get("ts") or "").strip()
                command = str(item.get("command") or "").strip()
                response = str(item.get("response") or "").strip()
                if ts and command:
                    telegram_commands.append({"ts": ts, "command": command, "response": response})
        return cls(
            log_offset=int(raw.get("log_offset") or 0),
            telegram_offset=int(raw.get("telegram_offset") or 0),
            phase=str(raw.get("phase") or "unknown"),
            last_event=str(raw.get("last_event") or ""),
            connected_sent_pid=int(raw.get("connected_sent_pid") or 0) or None,
            connected_sent_at=str(raw.get("connected_sent_at") or ""),
            observed_lines=observed_lines,
            telegram_commands=telegram_commands[-MAX_COMMAND_AUDIT:],
        )

    def dump(self, path: Path) -> None:
        observed_lines = self.observed_lines or []
        telegram_commands = self.telegram_commands or []
        _write_json(
            path,
            {
                "log_offset": self.log_offset,
                "telegram_offset": self.telegram_offset,
                "phase": self.phase,
                "last_event": self.last_event,
                "connected_sent_pid": self.connected_sent_pid,
                "connected_sent_at": self.connected_sent_at,
                "observed_lines": observed_lines[-MAX_OBSERVED_LINES:],
                "telegram_commands": telegram_commands[-MAX_COMMAND_AUDIT:],
            },
        )


@dataclass
class LeaderboardPollState:
    last_snapshot: Dict[str, object] = field(default_factory=dict)
    consecutive_failures: int = 0
    failure_alerted: bool = False
    last_error: str = ""

    @classmethod
    def load(cls, path: Path) -> "LeaderboardPollState":
        raw = _read_json(path)
        if not isinstance(raw, dict):
            return cls()
        last_snapshot = raw.get("last_snapshot")
        if not isinstance(last_snapshot, dict):
            last_snapshot = {}
        return cls(
            last_snapshot=last_snapshot,
            consecutive_failures=int(raw.get("consecutive_failures") or 0),
            failure_alerted=bool(raw.get("failure_alerted") or False),
            last_error=str(raw.get("last_error") or ""),
        )

    def dump(self, path: Path) -> None:
        _write_json(
            path,
            {
                "last_snapshot": self.last_snapshot,
                "consecutive_failures": self.consecutive_failures,
                "failure_alerted": self.failure_alerted,
                "last_error": self.last_error,
            },
        )


class TelegramClient:
    def __init__(self) -> None:
        self.bot_token = _env(
            "EMAIL_GAME_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "TG_BOT_TOKEN",
        )
        self.chat_id = _env(
            "EMAIL_GAME_TELEGRAM_CHAT_ID",
            "TELEGRAM_CHAT_ID",
            "TG_CHAT_ID",
        )
        self.enabled = bool(self.bot_token and self.chat_id)
        self._warned_disabled = False
        self._warned_invalid_config = False
        self._last_failure: Optional[str] = None
        self._last_failure_count = 0

    def warn_if_disabled(self) -> None:
        if self.enabled or self._warned_disabled:
            return
        self._warned_disabled = True
        print(
            "[monitor] Telegram config not found. Fill "
            "EMAIL_GAME_TELEGRAM_BOT_TOKEN and EMAIL_GAME_TELEGRAM_CHAT_ID in .env.local."
        )

    def _warn_if_config_looks_invalid(self) -> None:
        if self._warned_invalid_config or not self.enabled:
            return
        token_shape_ok = bool(re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", self.bot_token))
        chat_id_shape_ok = bool(re.match(r"^-?\d+$", self.chat_id))
        if token_shape_ok and chat_id_shape_ok:
            return
        self._warned_invalid_config = True
        print(
            "[monitor] Telegram config looks malformed: "
            f"token_shape_ok={token_shape_ok} chat_id_shape_ok={chat_id_shape_ok}"
        )

    def _warn_about_failure(self, message: str) -> None:
        if message == self._last_failure:
            self._last_failure_count += 1
            if self._last_failure_count == 2:
                print(f"[monitor] Repeating Telegram failure suppressed: {message}")
            return
        self._last_failure = message
        self._last_failure_count = 1
        print(f"[monitor] {message}")

    def _request_json(self, method: str, params: Dict[str, object]) -> Dict[str, object]:
        if not self.enabled:
            return {}
        query = urlencode(params).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            data=query,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=TELEGRAM_HTTP_TIMEOUT_SEC) as response:
            data = response.read().decode("utf-8", "replace")
        return json.loads(data)

    def get_updates(self, offset: int, timeout: int = 30) -> List[Dict[str, object]]:
        if not self.enabled:
            return []
        try:
            data = self._request_json(
                "getUpdates",
                {"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message"])},
            )
            if not data.get("ok"):
                return []
            result = data.get("result")
            return result if isinstance(result, list) else []
        except Exception as exc:
            self._warn_about_failure(f"Telegram getUpdates failed: {exc}")
            return []

    def latest_update_offset(self) -> int:
        updates = self.get_updates(0, timeout=0)
        if not updates:
            return 0
        latest = max(
            (int(update.get("update_id") or 0) for update in updates),
            default=0,
        )
        return latest + 1 if latest >= 0 else 0

    def send(
        self,
        message: str,
        parse_mode: str = TELEGRAM_PARSE_MODE,
        chat_id: Optional[str] = None,
        reply_markup: Optional[Dict[str, object]] = None,
    ) -> bool:
        message = message.strip()
        if not message:
            return False
        if not self.enabled:
            self.warn_if_disabled()
            print(f"[monitor] {message}")
            return False
        self._warn_if_config_looks_invalid()

        payload_dict: Dict[str, object] = {
            "chat_id": chat_id or self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            payload_dict["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        payload = urlencode(payload_dict).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                return 200 <= getattr(response, "status", 200) < 300
        except Exception as exc:
            body = ""
            code = getattr(exc, "code", None)
            if hasattr(exc, "read"):
                try:
                    body = exc.read().decode("utf-8", "replace").strip()
                except Exception:
                    body = ""
            failure = "Telegram send failed"
            if code is not None:
                failure += f" ({code})"
            if body:
                failure += f": {body}"
            else:
                failure += f": {exc}"
            if code == 400 and "chat not found" in body.lower():
                failure = (
                    "Telegram send failed (400): chat not found. "
                    "Verify EMAIL_GAME_TELEGRAM_CHAT_ID and that the bot was started in that chat."
                )
            self._warn_about_failure(failure)
            return False


class EmailGameMonitor:
    def __init__(self, log_file: Path, state_file: Path) -> None:
        self.log_file = log_file
        self.state_file = state_file
        self.events_file = EVENTS_FILE
        self.leaderboard_state_file = LEADERBOARD_STATE_FILE
        self.telegram = TelegramClient()
        self.state = MonitorState.load(state_file)
        self.leaderboard_state = LeaderboardPollState.load(self.leaderboard_state_file)
        self._observed_lines: Deque[ObservedLine] = deque(maxlen=MAX_OBSERVED_LINES)
        self._line_buffer: Deque[str] = deque(maxlen=MAX_SEEN_LINES)
        self._sent_event_keys: set[Tuple[str, str]] = set()
        self._agent_name = _env("EMAIL_GAME_AGENT_NAME") or "letlhogonolo_fanampe"
        self._server_url = _env("EMAIL_GAME_SERVER")
        self._leaderboard_poll_seconds = self._leaderboard_poll_interval_seconds()
        self._running = True
        self._lock = threading.Lock()
        self._last_log_reconnect_monotonic = 0.0
        self._last_heartbeat_monotonic = 0.0
        self._load_observed_lines()
        self._seed_observed_lines()
        self._seed_structured_events_from_observed_lines()

    def run(self) -> int:
        print(f"[monitor] watching {self.log_file}")
        print(f"[monitor] leaderboard polling every {self._leaderboard_poll_seconds}s")
        self.telegram.warn_if_disabled()
        if self.state.telegram_offset <= 0 and self.telegram.enabled:
            seeded_offset = self.telegram.latest_update_offset()
            if seeded_offset:
                self.state.telegram_offset = seeded_offset
        self._sync_start_offset()
        self.state.phase = self._derive_phase()
        self._persist_state()
        self._send_connected_once()

        thread = threading.Thread(target=self._telegram_loop, name="telegram-command-loop", daemon=True)
        thread.start()
        leaderboard_thread = threading.Thread(
            target=self._leaderboard_loop,
            name="leaderboard-poll-loop",
            daemon=True,
        )
        leaderboard_thread.start()

        try:
            while self._running:
                self._poll_once()
                self._heartbeat_once()
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n[monitor] shutting down")
        finally:
            self._running = False
            self._persist_state()
        return 0

    def _connected_message(self) -> str:
        return (
            "✅ <b>Email Game Monitor Connected</b>\n\n"
            f"Agent: <code>{html_escape(self._agent_name, quote=False)}</code>"
        )

    def _send_connected_once(self) -> None:
        pid = os.getpid()
        if self.state.connected_sent_pid == pid:
            print(f"[monitor] connected heartbeat already sent for pid {pid}")
            return
        self._telegram_send(self._connected_message())
        self.state.connected_sent_pid = pid
        self.state.connected_sent_at = _now_sast().isoformat()
        self._persist_state()

    def _heartbeat_once(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_monotonic < 60.0:
            return
        self._last_heartbeat_monotonic = now
        self.state.phase = self._derive_phase()
        self._persist_state()
        print(
            "[monitor] heartbeat "
            f"pid={os.getpid()} phase={self._derive_phase()} "
            f"log_age={self._format_age(self._log_file_age_seconds())}"
        )
        self._maybe_send_budget_alert()

    def _leaderboard_poll_interval_seconds(self) -> int:
        raw = _env("EMAIL_GAME_LEADERBOARD_POLL_SECONDS")
        if not raw:
            return DEFAULT_LEADERBOARD_POLL_SECONDS
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_LEADERBOARD_POLL_SECONDS
        return max(1, value)

    def _persist_state(self) -> None:
        with self._lock:
            self.state.observed_lines = [entry.to_json() for entry in self._observed_lines]
            self.state.dump(self.state_file)

    def _persist_leaderboard_state(self) -> None:
        with self._lock:
            self.leaderboard_state.dump(self.leaderboard_state_file)

    def _load_observed_lines(self) -> None:
        for raw in self.state.observed_lines or []:
            entry = ObservedLine.from_json(raw)
            if entry is not None:
                self._observed_lines.append(entry)
                self._line_buffer.append(entry.text)

    def _record_line(self, text: str, ts: Optional[datetime] = None) -> ObservedLine:
        entry = ObservedLine(ts=ts or _now_sast(), text=_redact(text).strip())
        self._observed_lines.append(entry)
        return entry

    def _append_structured_event(self, event: StructuredEvent) -> None:
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        payload = event.to_json()
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _read_structured_events(self, limit: int = MAX_EVENT_READ_LINES) -> List[StructuredEvent]:
        if not self.events_file.exists():
            return []
        try:
            lines: Deque[str] = deque(maxlen=limit)
            with self.events_file.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    if raw.strip():
                        lines.append(raw)
        except OSError:
            return []
        events: List[StructuredEvent] = []
        for line in lines:
            try:
                raw = json.loads(line)
            except Exception:
                continue
            event = StructuredEvent.from_json(raw)
            if event is not None:
                events.append(event)
        events.sort(key=lambda item: item.ts)
        return events

    def _latest_structured_event(self) -> Optional[StructuredEvent]:
        events = self._read_structured_events(limit=MAX_EVENT_READ_LINES)
        return events[-1] if events else None

    def _latest_structured_event_of_type(self, event_type: str) -> Optional[StructuredEvent]:
        events = [
            event
            for event in self._read_structured_events(limit=MAX_EVENT_READ_LINES)
            if event.type == event_type
        ]
        return events[-1] if events else None

    def _latest_match_activity_event(self) -> Optional[StructuredEvent]:
        match_types = {
            "round_started",
            "request_sent",
            "request_received",
            "signed_reply",
            "signature_submitted",
            "reminder",
            "game_started",
            "game_ended",
            "disconnect",
        }
        events = [
            event
            for event in self._read_structured_events(limit=MAX_EVENT_READ_LINES)
            if event.type in match_types
        ]
        return events[-1] if events else None

    def _record_structured_event_for_line(self, line: str, ts: datetime) -> Optional[StructuredEvent]:
        event = self._structured_event_from_line(line, ts)
        if event is not None:
            self._append_structured_event(event)
        return event

    def _structured_event_from_line(self, line: str, ts: datetime) -> Optional[StructuredEvent]:
        clean = _clean_log_text(line)
        if not clean:
            return None
        lower = clean.lower()
        round_id = self._extract_round_id(clean)
        round_value = int(round_id) if round_id and round_id.isdigit() else None

        def build(
            event_type: str,
            message: str,
            *,
            counterparty: str = "",
            model: str = "",
            tokens: Optional[Dict[str, int]] = None,
            cost_usd: Optional[float] = None,
        ) -> StructuredEvent:
            return StructuredEvent(
                ts=ts,
                type=event_type,
                round=round_value,
                agent=self._agent_name,
                counterparty=_redact(counterparty).strip(),
                message=_clean_log_text(message)[:220],
                model=model,
                tokens=tokens,
                cost_usd=cost_usd,
            )

        if "http request" in lower and "chat/completions" in lower:
            return build("llm_call", "LLM chat completion request observed", model="gpt-4.1-mini")
        if "match found - game starting!" in clean or "in game - round 1" in lower:
            return build("game_started", "Game started")
        if "game over - between matches now" in lower:
            return build("game_ended", "Game ended")
        if round_value is not None and ("in game - round" in lower or re.search(r"\[info\]\s*round\s+\d+:", clean, re.IGNORECASE)):
            return build("round_started", f"Round {round_value} observed")

        sent_match = re.search(r"Sent signature request to ([^ ]+)", clean, re.IGNORECASE)
        if sent_match:
            return build("request_sent", "Signature request sent", counterparty=sent_match.group(1).strip())

        inbound_request = re.search(r"received from ([^:]+): .*?(?:Request for signature|Signature Request)", clean, re.IGNORECASE)
        if inbound_request and "response" not in lower and "declin" not in lower:
            return build("request_received", "Signature request received", counterparty=inbound_request.group(1).strip())

        signed_payload = re.search(r"Received signed payload: signer=([^ ]+)", clean, re.IGNORECASE)
        if signed_payload:
            return build("signed_reply", "Signed payload received", counterparty=signed_payload.group(1).strip())

        signed_message = re.search(r"received from ([^:]+): Signed Message", clean, re.IGNORECASE)
        if signed_message:
            return build("signed_reply", "Signed reply received", counterparty=signed_message.group(1).strip())

        signed_request = re.search(r"Signed request from ([^ ]+)", clean, re.IGNORECASE)
        if signed_request:
            return build("signed_reply", "Signature request response sent", counterparty=signed_request.group(1).strip())

        submit_round = re.search(r"submitted signature for round (\d+) from ([^ ]+)", clean, re.IGNORECASE)
        if submit_round:
            round_value = int(submit_round.group(1))
            return build("signature_submitted", "Submitted signature to moderator", counterparty=submit_round.group(2).strip())

        submitted = re.search(r"Submitted received signature from ([^ ]+)", clean, re.IGNORECASE)
        if submitted:
            return build("signature_submitted", "Submitted signature to moderator", counterparty=submitted.group(1).strip())

        if "action completion reminder" in lower:
            return build("reminder", "Action completion reminder received")

        if "disconnected" in lower or "connection dropped" in lower or "connection error" in lower:
            return build("disconnect", "Agent disconnect/connectivity event observed")

        return None

    def _seed_observed_lines(self) -> None:
        if self._observed_lines or not self.log_file.exists():
            return
        lines: List[str] = []
        with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line:
                    lines.append(line)
        for line in lines[-MAX_OBSERVED_LINES:]:
            self._line_buffer.append(line)
            self._record_line(line)
        self._persist_state()

    def _seed_structured_events_from_observed_lines(self) -> None:
        existing_types = {event.type for event in self._read_structured_events(limit=MAX_EVENT_READ_LINES)}
        match_event_types = {
            "llm_call",
            "round_started",
            "request_sent",
            "request_received",
            "signed_reply",
            "signature_submitted",
            "reminder",
            "game_started",
            "game_ended",
            "disconnect",
        }
        if existing_types & match_event_types:
            return
        seeded = 0
        for entry in self._observed_lines:
            event = self._structured_event_from_line(entry.text, entry.ts)
            if event is None:
                continue
            self._append_structured_event(event)
            seeded += 1
        if seeded:
            print(f"[monitor] seeded {seeded} structured event(s) from observed state")

    def _recent_observed_entries(self, limit: int) -> List[ObservedLine]:
        return list(self._observed_lines)[-limit:]

    def _structured_event_entries(self, limit: int = MAX_EVENT_READ_LINES) -> List[ObservedLine]:
        entries: List[ObservedLine] = []
        for event in self._read_structured_events(limit=limit):
            text = self._structured_event_as_log_line(event)
            if text:
                entries.append(ObservedLine(ts=event.ts, text=text))
        return entries

    def _structured_event_as_log_line(self, event: StructuredEvent) -> str:
        round_suffix = f" (Round {event.round})" if event.round is not None else ""
        if event.type == "game_started":
            return "✅ Match found - game starting!"
        if event.type == "game_ended":
            return "Game over - between matches now"
        if event.type == "round_started" and event.round is not None:
            return f"🎮 IN GAME - Round {event.round}"
        if event.type == "request_sent" and event.counterparty:
            return f"Sent signature request to {event.counterparty}"
        if event.type == "request_received" and event.counterparty:
            return f"received from {event.counterparty}: Signature Request{round_suffix}"
        if event.type == "signed_reply" and event.counterparty:
            return f"received from {event.counterparty}: Signed Message{round_suffix}"
        if event.type == "signature_submitted":
            if event.round is not None and event.counterparty:
                return f"submitted signature for round {event.round} from {event.counterparty}"
            if event.counterparty:
                return f"Submitted received signature from {event.counterparty}"
        if event.type == "reminder":
            return f"received from system_reminder: Action Completion Reminder{round_suffix}"
        if event.type == "disconnect":
            return "disconnected"
        return ""

    def _read_log_tail_entries(self, limit: int) -> List[ObservedLine]:
        if not self.log_file.exists():
            return []
        try:
            mtime = datetime.fromtimestamp(self.log_file.stat().st_mtime, tz=timezone.utc).astimezone(SAST_TZ)
            lines: Deque[str] = deque(maxlen=max(limit * 3, limit))
            with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = _clean_log_text(raw_line)
                    if line:
                        lines.append(line)
        except OSError:
            return []
        return [ObservedLine(ts=mtime, text=line) for line in list(lines)[-limit:]]

    def _log_file_age_seconds(self) -> Optional[float]:
        if not self.log_file.exists():
            return None
        try:
            return max(0.0, time.time() - self.log_file.stat().st_mtime)
        except OSError:
            return None

    def _log_file_mtime_text(self) -> str:
        if not self.log_file.exists():
            return "missing"
        try:
            mtime = datetime.fromtimestamp(self.log_file.stat().st_mtime, tz=timezone.utc).astimezone(SAST_TZ)
        except OSError:
            return "unavailable"
        return mtime.strftime("%H:%M:%S SAST")

    def _latest_observed_age_seconds(self) -> Optional[float]:
        latest = self._latest_significant_entry()
        if latest is None:
            return None
        return max(0.0, (_now_sast() - latest.ts).total_seconds())

    def _pane_indicates_quiescent_between_matches(self) -> bool:
        latest = self._latest_significant_entry()
        return bool(latest and self._is_match_end(latest.text))

    def _format_age(self, seconds: Optional[float]) -> str:
        if seconds is None:
            return "missing"
        seconds_int = max(0, int(seconds))
        if seconds_int < 60:
            return f"{seconds_int}s"
        minutes, rem_seconds = divmod(seconds_int, 60)
        if minutes < 60:
            return f"{minutes}m {rem_seconds}s"
        hours, rem_minutes = divmod(minutes, 60)
        return f"{hours}h {rem_minutes}m"

    def _agent_process_running(self) -> bool:
        return _process_running_pattern(r"scripts/run_custom_agent.py letlhogonolo_fanampe")

    def _seed_from_tmux_capture(self) -> int:
        captured = _tmux_capture_pane(TMUX_SESSION_AGENT, lines=120)
        if not captured:
            return 0
        observed_now = _now_sast()
        added = 0
        for line in captured:
            if not line or line in self._line_buffer:
                continue
            self._line_buffer.append(line)
            self._record_line(line, observed_now)
            self._record_structured_event_for_line(line, observed_now)
            added += 1
        if added:
            self.state.phase = self._derive_phase()
            self._persist_state()
        return added

    def _check_log_stream(
        self,
        reconnect: bool = False,
        refresh_capture: bool = False,
        force_reconnect: bool = False,
    ) -> LogStreamStatus:
        age_seconds = self._log_file_age_seconds()
        agent_running = self._agent_process_running()
        file_stale = bool(agent_running and (age_seconds is None or age_seconds > STALE_LOG_THRESHOLD_SEC))
        reconnected = False
        reconnect_error = ""
        if file_stale and refresh_capture:
            self._seed_from_tmux_capture()
        reconnect_due = force_reconnect or (time.monotonic() - self._last_log_reconnect_monotonic >= 60.0)
        if file_stale and reconnect and reconnect_due:
            reconnected, reconnect_error = _tmux_reconnect_log_pipe(self.log_file)
            self._last_log_reconnect_monotonic = time.monotonic()
            if reconnected:
                self._sync_start_offset()
                if refresh_capture:
                    self._seed_from_tmux_capture()
            else:
                reconnect_error = _redact(reconnect_error)
        pane_age_seconds = self._latest_observed_age_seconds()
        pane_observed = bool(self._observed_lines)
        pane_fresh = bool(pane_observed and pane_age_seconds is not None and pane_age_seconds <= STALE_LOG_THRESHOLD_SEC)
        pane_quiescent_between = bool(pane_observed and self._pane_indicates_quiescent_between_matches())
        stale = bool(file_stale and not pane_fresh and not pane_quiescent_between)
        return LogStreamStatus(
            exists=self.log_file.exists(),
            age_seconds=age_seconds,
            stale=stale,
            file_stale=file_stale,
            pane_age_seconds=pane_age_seconds,
            pane_observed=pane_observed,
            reconnected=reconnected,
            reconnect_error=reconnect_error,
        )

    def _leaderboard_loop(self) -> None:
        next_poll = time.monotonic()
        while self._running:
            now = time.monotonic()
            if now < next_poll:
                time.sleep(min(1.0, next_poll - now))
                continue
            next_poll = now + self._leaderboard_poll_seconds
            self._poll_leaderboard_once()

    def _poll_leaderboard_once(self) -> None:
        snapshot, error = self._fetch_leaderboard_snapshot()
        if snapshot is None:
            self.leaderboard_state.consecutive_failures += 1
            self.leaderboard_state.last_error = error or "Unknown leaderboard fetch failure"
            print(
                f"[monitor] leaderboard poll failed "
                f"({self.leaderboard_state.consecutive_failures} consecutive): {self.leaderboard_state.last_error}"
            )
            if self.leaderboard_state.consecutive_failures >= 3 and not self.leaderboard_state.failure_alerted:
                self.leaderboard_state.failure_alerted = True
                self._telegram_send(
                    f"{html_escape(_format_sast(), quote=False)} • "
                    "⚠️ <b>Email Game Leaderboard Update</b>\n\n"
                    "Leaderboard polling has failed 3 times in a row. I will keep retrying."
                )
            self._persist_leaderboard_state()
            return

        had_failures = self.leaderboard_state.consecutive_failures > 0
        self.leaderboard_state.consecutive_failures = 0
        self.leaderboard_state.last_error = ""

        previous = self.leaderboard_state.last_snapshot or {}
        if previous:
            changed, message = self._leaderboard_change_message(previous, snapshot)
            if changed and message:
                self._telegram_send(message)
        self.leaderboard_state.last_snapshot = snapshot
        self._persist_leaderboard_state()
        self._maybe_send_coach_alert("leaderboard")

        if had_failures:
            self.leaderboard_state.failure_alerted = False
            self._persist_leaderboard_state()
            self._telegram_send(
                f"{html_escape(_format_sast(), quote=False)} • "
                "✅ <b>Email Game Leaderboard Update</b>\n\n"
                "Leaderboard polling recovered and is working again."
            )

    def _sync_start_offset(self) -> None:
        if not self.log_file.exists():
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self.state.log_offset = 0
            self._persist_state()
            return

        size = self.log_file.stat().st_size
        if self.state.log_offset > size:
            self.state.log_offset = 0
        elif self.state.log_offset == 0:
            self.state.log_offset = size
        self._persist_state()

    def _poll_once(self) -> None:
        if not self.log_file.exists():
            self._check_log_stream(reconnect=True, refresh_capture=True)
            return

        size = self.log_file.stat().st_size
        if size < self.state.log_offset:
            self.state.log_offset = 0

        if size == self.state.log_offset:
            status = self._check_log_stream(reconnect=True, refresh_capture=True)
            if status.stale and self._derive_phase() not in {"waiting", "between matches"}:
                self._maybe_send_coach_alert("log_stale")
            return

        with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self.state.log_offset)
            chunk = handle.read()
            self.state.log_offset = handle.tell()
        self._persist_state()

        observed_now = _now_sast()
        for raw_line in chunk.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in self._line_buffer:
                continue
            self._line_buffer.append(line)
            self._record_line(line, observed_now)
            structured_event = self._record_structured_event_for_line(line, observed_now)
            if structured_event is not None and structured_event.type == "llm_call":
                self._budget().record_llm_call(observed_now)
            event = self._classify(line)
            if event:
                self._notify(event, observed_now)
                if event.kind == "disconnected":
                    self._maybe_send_coach_alert("disconnect")
            elif "Action Completion Reminder" in line:
                self._maybe_send_coach_alert("reminder")

    def _telegram_loop(self) -> None:
        if not self.telegram.enabled:
            return
        while self._running:
            updates = self.telegram.get_updates(self.state.telegram_offset, timeout=30)
            if updates:
                for update in updates:
                    if not self._running:
                        break
                    self._handle_update(update)
            if TELEGRAM_POLL_INTERVAL_SEC:
                time.sleep(TELEGRAM_POLL_INTERVAL_SEC)

    def _handle_update(self, update: Dict[str, object]) -> None:
        update_id = update.get("update_id")
        try:
            update_id_int = int(update_id)
        except Exception:
            return

        message = update.get("message")
        if not isinstance(message, dict):
            self.state.telegram_offset = update_id_int + 1
            self._persist_state()
            return

        chat = message.get("chat")
        if not isinstance(chat, dict):
            self.state.telegram_offset = update_id_int + 1
            self._persist_state()
            return

        chat_id = chat.get("id")
        text = str(message.get("text") or "").strip()
        parts = text.split() if text.startswith("/") else []
        command = parts[0].split("@", 1)[0] if parts else ""
        bot_to_bot = self._is_authorized_bot_to_bot_message(message, command)
        if str(chat_id) != self.telegram.chat_id and not bot_to_bot:
            self.state.telegram_offset = update_id_int + 1
            self._persist_state()
            return

        if command:
            reply_markup = None
            if command == "/dashboard":
                dashboard_url, refreshed = self._dashboard_tunnel_url(force_refresh=False)
                response = self._dashboard_text(url=dashboard_url, refreshed=refreshed)
                reply_markup = self._dashboard_reply_markup(url=dashboard_url)
            elif command == "/dashboard_refresh":
                dashboard_url, refreshed = self._dashboard_tunnel_url(force_refresh=True)
                response = self._dashboard_text(url=dashboard_url, refreshed=refreshed)
                reply_markup = self._dashboard_reply_markup(url=dashboard_url)
            elif command == "/dashboard_qa_report":
                response = self._dashboard_qa_report_text()
            elif command == "/dashboard_url":
                response = self._dashboard_url_text()
            else:
                response = self._dispatch_command(command, parts[1:])
            self._record_telegram_command(command, bool(response))
            if response:
                self._telegram_send(
                    response,
                    chat_id=str(chat_id) if bot_to_bot else None,
                    reply_markup=reply_markup,
                )

        self.state.telegram_offset = update_id_int + 1
        self._persist_state()

    def _is_authorized_bot_to_bot_message(self, message: Dict[str, object], command: str) -> bool:
        if command not in BOT_TO_BOT_SAFE_COMMANDS:
            return False
        sender = message.get("from")
        if not isinstance(sender, dict):
            return False
        if not bool(sender.get("is_bot")):
            return False
        username = str(sender.get("username") or "").lstrip("@")
        expected = (_env("EMAIL_GAME_TESTER_BOT_USERNAME") or DEFAULT_TESTER_BOT_USERNAME).lstrip("@")
        return bool(username and expected and username.lower() == expected.lower())

    def _record_telegram_command(self, command: str, response_present: bool) -> None:
        safe_command = command if re.fullmatch(r"/[A-Za-z0-9_]+", command) else "/unknown"
        self.state.telegram_commands.append(
            {
                "ts": _now_sast().isoformat(),
                "command": safe_command,
                "response": "yes" if response_present else "no",
            }
        )
        self.state.telegram_commands = self.state.telegram_commands[-MAX_COMMAND_AUDIT:]

    def _dispatch_command(self, command: str, args: List[str]) -> str:
        if command == "/help":
            return self._help_text()
        if command == "/dashboard":
            dashboard_url, refreshed = self._dashboard_tunnel_url(force_refresh=False)
            return self._dashboard_text(url=dashboard_url, refreshed=refreshed)
        if command == "/dashboard_url":
            return self._dashboard_url_text()
        if command == "/dashboard_refresh":
            dashboard_url, refreshed = self._dashboard_tunnel_url(force_refresh=True)
            return self._dashboard_text(url=dashboard_url, refreshed=refreshed)
        if command == "/dashboard_qa_report":
            return self._dashboard_qa_report_text()
        if command == "/status":
            return self._status_text()
        if command == "/logs":
            return self._logs_text()
        if command == "/match":
            return self._match_text()
        if command == "/tail":
            return self._tail_text(args)
        if command == "/leaderboard":
            return self._leaderboard_text(args)
        if command == "/rank":
            return self._rank_text()
        if command == "/participants":
            return self._participants_text()
        if command == "/readiness":
            return self._readiness_text()
        if command == "/budget":
            return self._budget_text()
        if command == "/usage":
            return self._usage_text()
        if command == "/reconnectlog":
            return self._reconnect_log_text()
        if command in ("/why", "/reminders"):
            return self._reminders_text()
        if command == "/version":
            return self._version_text()
        if command == "/preflight":
            return self._preflight_text()
        if command == "/coach":
            return self._coach_text()
        if command == "/recommend":
            return self._recommend_text()
        if command == "/reviewmatch":
            return self._reviewmatch_text()
        if command == "/metrics":
            return self._metrics_text()
        if command == "/startagent":
            return self._start_agent_text()
        if command == "/restartagent":
            return self._restart_agent_text()
        if command == "/stopagent":
            return self._stop_agent_text()
        return self._help_text()

    def _help_text(self) -> str:
        return (
            "🎮 <b>Email Game Control</b>\n\n"
            "<b>Agent</b>\n"
            "/dashboard — open the race control dashboard\n"
            "/dashboard_refresh — refresh the dashboard tunnel\n"
            "/dashboard_qa_report — resend the latest QA screenshots\n"
            "/status — current state\n"
            "/startagent — start if idle\n"
            "/restartagent — safe restart\n"
            "/stopagent — stop only if safe\n\n"
            "<b>Monitoring</b>\n"
            "/logs — latest match summary\n"
            "/match — latest match only\n"
            "/why — explain recent reminders\n"
            "/reminders — same as /why\n"
            "/tail — raw redacted tail\n"
            "/leaderboard — current ranking\n"
            "/leaderboard full — full ranking if exposed\n"
            "/rank — my rank and gaps\n"
            "/participants — leaderboard visibility\n"
            "/readiness — competition readiness report\n"
            "/budget — LLM budget and remaining estimate\n"
            "/usage — recent LLM call usage\n"
            "/coach — concise performance analysis\n"
            "/recommend — next recommended Codex goal\n"
            "/reviewmatch — latest match diagnosis\n"
            "/metrics — numeric performance summary\n"
            "/reconnectlog — reconnect live log pipe\n"
            "/version — branch and commit\n"
            "/preflight — safe checks\n"
            "/help — show this menu\n\n"
            "Note: house_bot_* are built-in competition bots/opponents."
        )

    def _telegram_send(
        self,
        message: str,
        chat_id: Optional[str] = None,
        reply_markup: Optional[Dict[str, object]] = None,
    ) -> None:
        for chunk in _chunk_text(message):
            self.telegram.send(
                chunk,
                parse_mode=TELEGRAM_PARSE_MODE,
                chat_id=chat_id,
                reply_markup=reply_markup,
            )
            reply_markup = None

    def _coach(self) -> EmailGameCoach:
        return EmailGameCoach(
            log_file=self.log_file,
            monitor_state_file=self.state_file,
            leaderboard_state_file=self.leaderboard_state_file,
            event_store_file=EVENTS_FILE,
        )

    def _budget(self) -> EmailGameBudget:
        return EmailGameBudget(
            log_file=self.log_file,
            monitor_state_file=self.state_file,
            leaderboard_state_file=self.leaderboard_state_file,
            budget_state_file=BUDGET_STATE_FILE,
            event_store_file=EVENTS_FILE,
        )

    def _coach_text(self) -> str:
        return self._coach().telegram_coach_text()

    def _dashboard_tunnel_url(self, force_refresh: bool = False) -> Tuple[str, bool]:
        url = _read_text_file(DASHBOARD_URL_FILE)
        if url and not force_refresh and self._dashboard_url_is_alive(url):
            return url, False
        refreshed = self._refresh_dashboard_tunnel()
        if refreshed:
            return refreshed, True
        return (url, False) if url else ("", False)

    def _dashboard_url_is_alive(self, url: str) -> bool:
        if not url:
            return False
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=10) as response:
                status = int(getattr(response, "status", 200) or 200)
                body = response.read(4096).decode("utf-8", "replace")
        except Exception as exc:
            status = int(getattr(exc, "code", 0) or 0)
            body = ""
            if hasattr(exc, "read"):
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
        haystack = f"{status}\n{body}".lower()
        if "error 1033" in haystack or "cloudflare error 1033" in haystack:
            return False
        return status == 200

    def _wait_for_dashboard_url(self, timeout: int = 30) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if DASHBOARD_TUNNEL_LOG_FILE.exists():
                url = _extract_public_dashboard_url(DASHBOARD_TUNNEL_LOG_FILE.read_text(encoding="utf-8", errors="replace"))
                if url:
                    return url
            time.sleep(0.5)
        return ""

    def _refresh_dashboard_tunnel(self) -> str:
        try:
            DASHBOARD_TUNNEL_LOG_FILE.unlink()
        except FileNotFoundError:
            pass
        start_result = _run_command(["bash", "scripts/start_emailgame_dashboard.sh"], timeout=30)
        if start_result.returncode != 0 and not _tmux_has_session(DASHBOARD_TUNNEL_SESSION):
            return ""
        base_url = self._wait_for_dashboard_url(timeout=30)
        if not base_url:
            return ""
        url = _build_protected_dashboard_url(base_url)
        if not url:
            return ""
        _write_text_file(DASHBOARD_URL_FILE, url)
        return url

    def _dashboard_url_text(self) -> str:
        url, _ = self._dashboard_tunnel_url(force_refresh=False)
        if not url:
            return (
                "🏁 <b>Email Game Race Control</b>\n\n"
                "Dashboard is running locally, but no mobile link is active yet.\n"
                "Ask Codex to start the dashboard tunnel."
            )
        return (
            "🏁 <b>Email Game Race Control</b>\n\n"
            f"Open dashboard:\n<code>{html_escape(url, quote=False)}</code>"
        )

    def _dashboard_qa_report_text(self) -> str:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "dashboard_frontend_qa.py"),
            "--send-report",
            "--force",
        ]
        completed = _run_command(command, timeout=300)
        stdout = _clean_log_text(completed.stdout)
        stderr = _clean_log_text(completed.stderr)
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit {completed.returncode}"
            return (
                "Dashboard QA report resend failed.\n\n"
                f"<pre>{html_escape(detail, quote=False)}</pre>"
            )
        return (
            "Dashboard QA report resent.\n\n"
            f"<pre>{html_escape(stdout or 'No output.', quote=False)}</pre>"
        )

    def _dashboard_text(self, url: str = "", refreshed: bool = False) -> str:
        if not url:
            return (
                "🏁 <b>Email Game Race Control</b>\n\n"
                "Dashboard link could not be refreshed yet.\n"
                "Ask Codex to restart the tunnel."
            )
        if refreshed:
            lead = "Dashboard link refreshed."
        else:
            lead = "Dashboard link is live."
        return (
            "🏁 <b>Email Game Race Control</b>\n\n"
            f"{lead}\n"
            "Tap Open Race Control Dashboard.\n"
            f"Open dashboard:\n<code>{html_escape(url, quote=False)}</code>"
        )

    def _dashboard_reply_markup(self, url: str = "") -> Optional[Dict[str, object]]:
        if not url:
            return None
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "Open Race Control Dashboard",
                        "url": url,
                    }
                ]
            ]
        }

    def _budget_text(self) -> str:
        return self._budget().budget_text()

    def _usage_text(self) -> str:
        return self._budget().usage_text()

    def _recommend_text(self) -> str:
        return self._coach().telegram_recommend_text()

    def _reviewmatch_text(self) -> str:
        self._check_log_stream(reconnect=True, refresh_capture=True)
        summary = self._latest_match_summary()
        if summary is None:
            return "No match diagnosis available yet."

        rounds = self._sorted_round_summaries(summary)
        requests_sent = sum(
            max(len(round_summary.requested_from), round_summary.request_targets or 0)
            for round_summary in rounds.values()
        )
        signed_replies = sum(len(round_summary.signed_from) for round_summary in rounds.values())
        submissions = sum(1 for round_summary in rounds.values() if round_summary.submitted is True)
        reminders = sum(round_summary.reminders for round_summary in rounds.values())
        lines = [
            "<b>Latest Match Diagnosis</b>",
            "",
            f"Started: {_format_sast(summary.started_at)}",
            f"Status: {'ended' if summary.ended else 'in progress'}",
            f"Rounds: {len(rounds)}",
            f"Requests sent: {requests_sent}",
            f"Signed replies received: {signed_replies}",
            f"Signatures submitted: {submissions}",
            f"Action reminders: {reminders}",
            "",
            "<b>Rounds:</b>",
        ]
        for round_id, round_summary in rounds.items():
            sent_count = max(len(round_summary.requested_from), round_summary.request_targets or 0)
            lines.append(
                f"- Round {html_escape(round_id, quote=False)}: "
                f"sent {sent_count}, replies {len(round_summary.signed_from)}, "
                f"submitted {1 if round_summary.submitted is True else 0}, "
                f"reminders {round_summary.reminders}"
            )
        return "\n".join(lines)

    def _metrics_text(self) -> str:
        return self._coach().telegram_metrics_text()

    def _maybe_send_coach_alert(self, reason: str) -> None:
        try:
            message = self._coach().maybe_alert_text(reason)
        except Exception as exc:
            print(f"[monitor] coach analysis failed: {_redact(str(exc))}")
            return
        if message:
            self._telegram_send(message)

    def _maybe_send_budget_alert(self) -> None:
        try:
            message = self._budget().maybe_alert_text()
        except Exception as exc:
            print(f"[monitor] budget analysis failed: {_redact(str(exc))}")
            return
        if message:
            self._telegram_send(message)

    def _notify(self, event: Event, ts: Optional[datetime] = None) -> None:
        key = (event.kind, event.text)
        if key in self._sent_event_keys:
            return
        self._sent_event_keys.add(key)
        self.state.last_event = event.text
        self._persist_state()
        prefix = _format_sast(ts)
        self._telegram_send(f"{html_escape(prefix, quote=False)} • {html_escape(event.text, quote=False)}")

    def _classify(self, line: str) -> Optional[Event]:
        clean = _redact(line)

        if "Starting custom agent" in line or "Loaded CustomAgent" in line:
            self.state.phase = "between_matches"
            return Event("started", f"Email Game agent started: {self._agent_name}")

        queue_match = re.search(r"Joined matchmaking queue \(position (\d+)\)", line)
        if queue_match:
            self.state.phase = "queued"
            return Event("queue", f"Agent joined matchmaking queue at position {queue_match.group(1)}")

        if "✅ Match found - game starting!" in line or "🎮 IN GAME - Round 1" in line:
            self.state.phase = "in_game"
            return Event("match_started", f"Match started for {self._agent_name}")

        if "Round " in line and " moderator message" not in line and "IN GAME - Round" in line:
            round_match = re.search(r"Round\s+(\d+)", line)
            if round_match:
                self.state.phase = "in_game"
                return Event("moderator", f"Moderator message received for round {round_match.group(1)}")

        if "Sent signature request to " in line:
            self.state.phase = "in_game"
            agent = line.rsplit("Sent signature request to ", 1)[-1].strip()
            return Event("signature_request", f"Signature request sent to {agent}")

        if "Signed request from " in line:
            self.state.phase = "in_game"
            agent = line.rsplit("Signed request from ", 1)[-1].strip()
            return Event("signed", f"Signature received and signed request sent for {agent}")

        if "Submitted received signature from " in line:
            self.state.phase = "in_game"
            agent = line.rsplit("Submitted received signature from ", 1)[-1].strip()
            return Event("signature_submitted", f"Received signature from {agent} and submitted it")

        if "Game over - between matches now" in line:
            self.state.phase = "between_matches"
            return Event("game_finished", "Game finished; agent is back between matches")

        if (
            "disconnected" in line.lower()
            or "connection dropped" in line.lower()
            or "connection error" in line.lower()
        ):
            self.state.phase = "disconnected"
            return Event("disconnected", f"Agent disconnected or reconnected: {clean}")

        error_markers = (
            "Traceback",
            "LLM consumer error",
            "on_new_game() error",
            "Error handling messages",
            "Could not reach the game server",
            "Could not join",
            "LLM Driver error",
            "Failed to import",
            "Failed to send message",
        )
        if any(marker in line for marker in error_markers) or re.search(r"\bERROR\b", line):
            self.state.phase = "error"
            return Event("error", f"Agent error: {clean}")

        return None

    def _latest_significant_entry(self) -> Optional[ObservedLine]:
        entries = self._structured_event_entries(MAX_STATUS_LINES) or self._recent_observed_entries(MAX_STATUS_LINES)
        for entry in reversed(entries):
            if self._looks_interesting(entry.text):
                return entry
        return None

    def _latest_significant_line(self) -> str:
        entry = self._latest_significant_entry()
        return entry.text if entry else ""

    def _latest_round_number(self) -> str:
        summary = self._latest_match_summary()
        if summary and summary.rounds:
            rounds = sorted(summary.rounds.keys(), key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
            return rounds[-1]
        recent = self._recent_observed_entries(MAX_STATUS_LINES)
        for entry in reversed(recent):
            match = re.search(r"Round\s+(\d+)", entry.text)
            if match:
                return match.group(1)
        return "n/a"

    def _derive_phase(self) -> str:
        recent = self._structured_event_entries(MAX_STATUS_LINES) or self._recent_observed_entries(MAX_STATUS_LINES)
        if not recent:
            return "waiting"
        combined = "\n".join(entry.text for entry in recent).lower()
        if "game over - between matches now" in combined:
            return "between matches"
        if any(word in combined for word in ("traceback", "llm consumer error", "on_new_game() error", "error handling messages", "could not reach the game server", "could not join", "llm driver error", "failed to import", "failed to send message", "error")):
            return "crashed"
        if any(word in combined for word in ("disconnected", "connection dropped", "connection error")):
            return "disconnected"
        if "in game" in combined:
            return "in game"
        if "joined matchmaking queue" in combined:
            return "waiting"
        return "waiting"

    def _tail_text(self, args: List[str]) -> str:
        log_status = self._check_log_stream(reconnect=True, refresh_capture=True)
        lines = self._read_log_tail_entries(MAX_TAIL_LINES) if not log_status.file_stale else []
        if not lines:
            lines = self._recent_observed_entries(MAX_TAIL_LINES)
        if not lines:
            return "No log lines available yet."
        display_lines = [
            entry
            for entry in lines
            if not SEPARATOR_RE.match(entry.text)
            and entry.text.strip() not in {"^[[A", "^[[B", "^[[C", "^[[D"}
            and "Watch your match" not in entry.text
            and "View leaderboard" not in entry.text
            and "the-email-game.fly.dev/leaderboard/testing" not in entry.text
            and "[watch link redacted]" not in entry.text
        ]
        if not display_lines:
            display_lines = lines
        if args and args[0].lower() == "raw":
            rendered = "\n".join(
                f"{_format_sast(entry.ts)} • {html_escape(entry.text, quote=False)}" for entry in display_lines
            )
            return f"📄 <b>Recent Redacted Log Tail</b>\n\n{rendered}"
        return "📄 <b>Recent Redacted Log Tail</b>\n\n" + "\n".join(
            f"• {_format_sast(entry.ts)} • {html_escape(entry.text, quote=False)}" for entry in display_lines
        )

    def _logs_text(self) -> str:
        self._check_log_stream(reconnect=True, refresh_capture=True)
        summary = self._latest_match_summary()
        if summary is None:
            return "No match summary available yet."
        return self._render_match_summary(
            summary,
            title="📋 <b>Recent Match Summary</b>",
            include_note=True,
            compact_reminders=True,
            idle_note=True,
        )

    def _match_text(self) -> str:
        summary = self._latest_match_summary()
        if summary is None:
            return "No match summary available yet."
        return self._render_match_summary(
            summary,
            title="📋 <b>Latest Match Summary</b>",
            include_note=False,
            compact_reminders=False,
            idle_note=True,
        )

    def _reminders_text(self) -> str:
        summary = self._latest_match_summary()
        if summary is None:
            return "No recent match activity found yet."

        reminder_rounds = [
            round_id
            for round_id, round_summary in self._sorted_round_summaries(summary).items()
            if round_summary.reminders > 0
        ]
        if not reminder_rounds:
            return (
                "⚠️ <b>Recent Reminders</b>\n\n"
                "No Action Completion Reminders were observed in the latest match.\n"
                "If they appear later, it usually means the agent still has pending signature work."
            )

        lines = [
            "⚠️ <b>Recent Reminders</b>",
            "",
            f"The latest match had <b>{html_escape(str(sum(r.reminders for r in summary.rounds.values())), quote=False)}</b> reminder(s).",
            "That usually means the agent still had pending work when the timer nudged it.",
            "",
            "Rounds with reminders:",
        ]
        for round_id in reminder_rounds:
            lines.append(f"• Round {html_escape(round_id, quote=False)}")
        lines.extend(
            [
                "",
                "Common causes:",
                "• a received signature still needed to be submitted",
                "• a signature request still needed a reply",
                "• the round timer reached the reminder threshold",
            ]
        )
        return "\n".join(lines)

    def _looks_interesting(self, line: str) -> bool:
        markers = (
            "Match found",
            "IN GAME",
            "Game over",
            "signature",
            "queue",
            "error",
            "disconnected",
            "Starting custom agent",
            "Loaded CustomAgent",
        )
        return any(marker.lower() in line.lower() for marker in markers)

    def _reconnect_log_text(self) -> str:
        self._seed_from_tmux_capture()
        reconnected, reconnect_error = _tmux_reconnect_log_pipe(self.log_file)
        marker_ts = _now_sast()
        self._append_structured_event(
            StructuredEvent(
                ts=marker_ts,
                type="reconnectlog",
                round=None,
                agent=self._agent_name,
                counterparty="",
                message="Log pipe reconnect requested",
            )
        )
        if reconnected:
            self._sync_start_offset()
            self._seed_from_tmux_capture()
        reconnect_error = _redact(reconnect_error)
        status = self._check_log_stream(reconnect=False, refresh_capture=True)
        pane_id = _tmux_pane_id(TMUX_SESSION_AGENT) or "unknown"
        pane_pid = _tmux_pane_pid(TMUX_SESSION_AGENT)
        pane_pid_text = str(pane_pid) if pane_pid else "unknown"
        latest_event = self._latest_structured_event()
        latest_event_text = _format_sast(latest_event.ts if latest_event else marker_ts)
        if reconnected:
            return (
                "✅ <b>Log pipe reconnected</b>\n\n"
                f"Pane: <code>{html_escape(pane_id, quote=False)}</code>\n"
                f"PID: <code>{html_escape(pane_pid_text, quote=False)}</code>\n"
                f"Log mtime: <b>{html_escape(self._log_file_mtime_text(), quote=False)}</b>\n"
                f"Log freshness: <b>{html_escape(self._format_age(status.age_seconds), quote=False)}</b>\n"
                f"Latest event: <b>{html_escape(latest_event_text, quote=False)}</b>"
            )
        if reconnect_error:
            return (
                "⚠️ <b>Log pipe reconnect failed</b>\n\n"
                f"Pane: <code>{html_escape(pane_id, quote=False)}</code>\n"
                f"PID: <code>{html_escape(pane_pid_text, quote=False)}</code>\n"
                f"Log mtime: <b>{html_escape(self._log_file_mtime_text(), quote=False)}</b>\n"
                f"Latest event: <b>{html_escape(latest_event_text, quote=False)}</b>\n"
                f"Error: <code>{html_escape(_clean_log_text(reconnect_error), quote=False)}</code>"
            )
        return (
            "⚠️ <b>Log pipe reconnect had no result</b>\n\n"
            f"Pane: <code>{html_escape(pane_id, quote=False)}</code>\n"
            f"PID: <code>{html_escape(pane_pid_text, quote=False)}</code>\n"
            f"Log mtime: <b>{html_escape(self._log_file_mtime_text(), quote=False)}</b>\n"
            f"Log freshness: <b>{html_escape(self._format_age(status.age_seconds), quote=False)}</b>\n"
            f"Latest event: <b>{html_escape(latest_event_text, quote=False)}</b>"
        )

    def _status_text(self) -> str:
        now_sast = _now_sast()
        log_status = self._check_log_stream(reconnect=True, refresh_capture=True)
        branch = self._branch_text()
        commit = self._commit_text()
        agent_running = self._agent_process_running()
        monitor_running = _process_running_pattern(r"scripts/monitor_emailgame_telegram.py")
        phase = self._derive_phase()
        latest_entry = self._latest_significant_entry()
        latest_line = latest_entry.text if latest_entry else "No notable log line found yet."
        round_text = self._latest_round_number()
        summary = self._latest_match_summary()
        state_label, state_icon = self._state_card(agent_running, phase)
        process_icon = "✅" if agent_running else "🔴"
        monitor_icon = "✅" if monitor_running else "🔴"
        updated = _format_sast(latest_entry.ts if latest_entry else None)
        latest_event = self._latest_structured_event()
        latest_event_age = (
            max(0.0, (now_sast - latest_event.ts).total_seconds()) if latest_event is not None else None
        )
        latest_llm_event = self._latest_structured_event_of_type("llm_call")
        latest_match_activity = self._latest_match_activity_event()
        last_completed_match_time = summary.started_at if summary and summary.ended else None
        idle_waiting = bool(
            agent_running
            and phase in {"waiting", "between matches"}
            and latest_event is not None
            and latest_event_age is not None
            and latest_event_age <= STALE_LOG_THRESHOLD_SEC
            and (latest_match_activity is None or latest_match_activity.ts < latest_event.ts)
        )
        summary_line = self._match_summary_line(summary)
        reminder_count = sum(round_summary.reminders for round_summary in summary.rounds.values()) if summary else 0
        pane_id = _tmux_pane_id(TMUX_SESSION_AGENT) or "unknown"
        pane_pid = _tmux_pane_pid(TMUX_SESSION_AGENT)
        pipe_connected = _tmux_pane_pipe_connected(TMUX_SESSION_AGENT)
        waiting_alone = self._waiting_alone()
        source = "live log file" if not log_status.file_stale else ("tmux pane capture" if log_status.pane_observed else "stale log file")
        warning_lines: List[str] = []
        if agent_running and phase in {"waiting", "between matches"}:
            warning_lines.append("Agent is waiting for a match. No new match activity observed.")
        elif log_status.file_stale and log_status.pane_observed:
            warning_lines.append("ℹ️ Live log file stale; monitor is using pane capture")
        elif log_status.stale:
            warning_lines.append("⚠️ Log stream stale")
        if waiting_alone:
            warning_lines.append("Waiting for match; leaderboard may stay flat until other agents are online.")
        if log_status.reconnected:
            warning_lines.append("✅ tmux pipe reattached")
        elif log_status.reconnect_error:
            warning_lines.append(
                f"Reconnect error: <code>{html_escape(_clean_log_text(log_status.reconnect_error), quote=False)}</code>"
            )
        lines = [
            "🎮 <b>Email Game Status</b>",
            "",
            f"Process: {process_icon} <b>{'running' if agent_running else 'stopped'}</b>",
            f"State: {state_icon} <b>{html_escape(state_label, quote=False)}</b>",
            f"State source: <b>{html_escape(source, quote=False)}</b>",
            f"Query time: <b>{html_escape(_format_sast(now_sast), quote=False)}</b>",
            f"Log freshness: <b>{html_escape(self._format_age(log_status.age_seconds), quote=False)}</b>",
            f"Log mtime: <b>{html_escape(self._log_file_mtime_text(), quote=False)}</b>",
            f"Pane observed freshness: <b>{html_escape(self._format_age(log_status.pane_age_seconds), quote=False)}</b>",
            f"Structured event freshness: <b>{html_escape(self._format_age(latest_event_age), quote=False)}</b>",
            f"Last structured event: <b>{html_escape(_format_sast(latest_event.ts) if latest_event else 'n/a', quote=False)}</b>",
            f"Last LLM call: <b>{html_escape(_format_sast(latest_llm_event.ts) if latest_llm_event else 'n/a', quote=False)}</b>",
            f"Last completed match: <b>{html_escape(_format_sast(last_completed_match_time) if last_completed_match_time else 'n/a', quote=False)}</b>",
            f"Pipe connected: <b>{html_escape(self._format_yes_no_unknown(pipe_connected), quote=False)}</b>",
            f"Pipe reconnected: <b>{'yes' if log_status.reconnected else 'no'}</b>",
            f"Pane: <code>{html_escape(pane_id, quote=False)}</code> / PID <code>{html_escape(str(pane_pid) if pane_pid else 'unknown', quote=False)}</code>",
            f"Latest round: <b>{html_escape(round_text, quote=False)}</b>",
            f"Last match log: <b>{html_escape(updated, quote=False)}</b>",
            f"Latest match: <i>{html_escape(summary_line, quote=False)}</i>",
            f"Reminder count: <b>{html_escape(str(reminder_count), quote=False)}</b>",
            f"Branch: <code>{html_escape(branch, quote=False)}</code>",
            f"Commit: <code>{html_escape(commit, quote=False)}</code>",
            f"Monitor: {monitor_icon} running",
            f"Last match log line: <i>{html_escape(_format_sast(latest_entry.ts if latest_entry else None), quote=False)} • {html_escape(_clean_log_text(latest_line), quote=False)}</i>",
        ]
        if warning_lines:
            lines.extend(["", *warning_lines])
        return "\n".join(lines)

    def _match_active(self) -> bool:
        recent = self._recent_observed_entries(40)
        has_game = any("IN GAME" in entry.text or "Match found" in entry.text for entry in recent)
        finished = any("Game over - between matches now" in entry.text for entry in recent)
        return has_game and not finished

    def _waiting_alone(self) -> bool:
        data, _error = self._fetch_leaderboard_data()
        if not isinstance(data, dict):
            return False
        live = data.get("live")
        if not isinstance(live, dict):
            return False
        try:
            online = int(live.get("players"))
            in_game = int(live.get("in_game"))
            waiting = int(live.get("queued"))
        except Exception:
            return False
        return online == 1 and in_game == 0 and waiting == 1

    def _fetch_leaderboard_data(self) -> Tuple[Optional[Dict[str, object]], str]:
        if not self._server_url:
            return None, "EMAIL_GAME_SERVER is not set"
        url = self._server_url.rstrip("/") + "/api/leaderboard/testing"
        try:
            with urlopen(url, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            return None, str(exc)
        if not isinstance(data, dict):
            return None, "Testing leaderboard response was malformed"
        entries = data.get("leaderboard")
        if not isinstance(entries, list):
            return None, "Testing leaderboard response was malformed"
        return data, ""

    def _leaderboard_entries(self, data: Dict[str, object]) -> List[Dict[str, object]]:
        entries = data.get("leaderboard") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _my_leaderboard_entry(self, entries: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
        return next((entry for entry in entries if entry.get("agent_id") == self._agent_name), None)

    def _entry_rank(self, entry: Optional[Dict[str, object]]) -> Optional[int]:
        if not isinstance(entry, dict):
            return None
        raw = str(entry.get("rank") or "").strip()
        return int(raw) if raw.isdigit() else None

    def _entry_score(self, entry: Optional[Dict[str, object]]) -> Optional[int]:
        if not isinstance(entry, dict):
            return None
        raw = str(entry.get("elo") or "").strip()
        return int(raw) if raw.lstrip("-").isdigit() else None

    def _gap_to_next_visible_rank(self, entries: List[Dict[str, object]], me: Optional[Dict[str, object]]) -> str:
        my_rank = self._entry_rank(me)
        my_score = self._entry_score(me)
        if my_rank is None or my_score is None:
            return "n/a"
        better = [
            entry
            for entry in entries
            if self._entry_rank(entry) is not None
            and self._entry_score(entry) is not None
            and self._entry_rank(entry) < my_rank
        ]
        if not better:
            return "0"
        next_entry = max(better, key=lambda entry: self._entry_rank(entry) or 0)
        next_score = self._entry_score(next_entry)
        if next_score is None:
            return "n/a"
        return str(max(next_score - my_score, 0))

    def _leaderboard_text(self, args: Optional[List[str]] = None) -> str:
        data, error = self._fetch_leaderboard_data()
        if data is None:
            return f"Failed to fetch testing leaderboard: {error}"
        entries = self._leaderboard_entries(data)
        full_requested = bool(args and args[0].lower() == "full")
        top = [entry for entry in entries[:5] if isinstance(entry, dict)]
        me = self._my_leaderboard_entry(entries)
        if full_requested:
            lines = ["🏆 <b>Testing Leaderboard Full</b>", "", f"Fetched: <b>{html_escape(_format_sast(), quote=False)}</b>", ""]
            if len(entries) <= 5:
                lines.append("Server currently exposes only Top 5 to this parser/source.")
                lines.append("")
            display = entries
        else:
            lines = ["🏆 <b>Testing Leaderboard</b>", "", f"Fetched: <b>{html_escape(_format_sast(), quote=False)}</b>", ""]
            display = top

        for entry in display:
            rank = html_escape(str(entry.get("rank", "?")), quote=False)
            agent_id_raw = str(entry.get("agent_id", "unknown"))
            agent_id = html_escape(self._leaderboard_agent_label(agent_id_raw), quote=False)
            score = html_escape(str(entry.get("elo", "?")), quote=False)
            if entry.get("agent_id") == self._agent_name:
                lines.append(f"{rank}. <b>{agent_id} — {score}</b>")
            else:
                lines.append(f"{rank}. {agent_id} — {score}")
        if isinstance(me, dict):
            rank = str(me.get("rank", "?"))
            score = str(me.get("elo", "?"))
            rank_int = int(rank) if str(rank).isdigit() else None
            gap_to_four = self._leaderboard_gap_to_rank(entries, 4, me)
            gap_to_one = self._leaderboard_gap_to_rank(entries, 1, me)
            lines.extend(
                [
                    "",
                    f"Your rank: <b>#{html_escape(rank, quote=False)}</b>",
                    f"Your score: <b>{html_escape(score, quote=False)}</b>",
                ]
            )
            lines.append(f"Gap to #4: <b>{html_escape(gap_to_four, quote=False)}</b>")
            lines.append(f"Gap to #1: <b>{html_escape(gap_to_one, quote=False)}</b>")
            if rank_int is not None:
                lines.append(f"Rank highlighted: <b>{html_escape(self._leaderboard_agent_label(self._agent_name), quote=False)}</b>")
        else:
            lines.extend(["", f"Your agent: <b>{html_escape(self._agent_name, quote=False)}</b> not visible on the board"])
        return "\n".join(lines)

    def _rank_text(self) -> str:
        data, error = self._fetch_leaderboard_data()
        if data is None:
            return f"Failed to fetch rank: {error}"
        entries = self._leaderboard_entries(data)
        me = self._my_leaderboard_entry(entries)
        if not isinstance(me, dict):
            return (
                "📍 <b>Email Game Rank</b>\n\n"
                f"Agent: <code>{html_escape(self._agent_name, quote=False)}</code>\n"
                "Rank: <b>not visible</b>\n"
                "Score: <b>n/a</b>\n"
                "Server currently exposes only Top 5 to this parser/source."
            )

        rank = str(me.get("rank", "?"))
        score = str(me.get("elo", "?"))
        gap_next = self._gap_to_next_visible_rank(entries, me)
        gap_one = self._leaderboard_gap_to_rank(entries, 1, me)
        leader = next((entry for entry in entries if self._entry_rank(entry) == 1), None)
        leader_label = self._leaderboard_agent_label(str(leader.get("agent_id") or "unknown")) if isinstance(leader, dict) else "n/a"
        one_moved = "unknown"
        previous_top = []
        if isinstance(self.leaderboard_state.last_snapshot, dict):
            previous_top_raw = self.leaderboard_state.last_snapshot.get("top5")
            if isinstance(previous_top_raw, list):
                previous_top = [entry for entry in previous_top_raw if isinstance(entry, dict)]
        previous_leader = next((entry for entry in previous_top if self._entry_rank(entry) == 1), None)
        if isinstance(leader, dict) and isinstance(previous_leader, dict):
            one_moved = "yes" if (
                leader.get("agent_id") != previous_leader.get("agent_id")
                or str(leader.get("elo")) != str(previous_leader.get("elo"))
            ) else "no"

        return "\n".join(
            [
                "📍 <b>Email Game Rank</b>",
                "",
                f"Your rank: <b>#{html_escape(rank, quote=False)}</b>",
                f"Your score: <b>{html_escape(score, quote=False)}</b>",
                f"Gap to next visible rank: <b>{html_escape(gap_next, quote=False)}</b>",
                f"Gap to #1: <b>{html_escape(gap_one, quote=False)}</b>",
                f"#1: <b>{html_escape(leader_label, quote=False)}</b>",
                f"#1 moved recently: <b>{html_escape(one_moved, quote=False)}</b>",
            ]
        )

    def _participants_text(self) -> str:
        data, error = self._fetch_leaderboard_data()
        if data is None:
            return f"Failed to fetch participants: {error}"
        entries = self._leaderboard_entries(data)
        live = data.get("live") if isinstance(data, dict) else {}
        if not isinstance(live, dict):
            live = {}
        total = len(entries)
        house_count = sum(1 for entry in entries if str(entry.get("agent_id") or "") in HOUSE_BOT_IDS)
        human_count = total - house_count
        visible_agents = ", ".join(
            self._leaderboard_agent_label(str(entry.get("agent_id") or "unknown"))
            for entry in entries[:5]
        ) or "none"
        full_exposed = "yes" if total > 5 else "no"
        visibility_note = (
            "Server currently exposes only Top 5 to this parser/source."
            if total <= 5
            else "Server exposes more than Top 5 to this parser/source."
        )
        lines = [
            "👥 <b>Email Game Participants</b>",
            "",
            f"Full leaderboard exposed: <b>{full_exposed}</b>",
            f"Total listed agents: <b>{html_escape(str(total), quote=False)}</b>",
            f"Visible agents: <b>{html_escape(str(min(total, 5)), quote=False)}</b>",
            f"House bots: <b>{html_escape(str(house_count), quote=False)}</b>",
            f"Likely human participants visible: <b>{html_escape(str(human_count), quote=False)}</b>",
            f"Online: <b>{html_escape(str(live.get('players', 'n/a')), quote=False)}</b>",
            f"In match: <b>{html_escape(str(live.get('in_game', 'n/a')), quote=False)}</b>",
            f"Waiting: <b>{html_escape(str(live.get('queued', 'n/a')), quote=False)}</b>",
            "",
            visibility_note,
            "",
            f"Visible list: {html_escape(visible_agents, quote=False)}",
        ]
        return "\n".join(lines)

    def _identity_key_present(self) -> bool:
        key_dir = Path.home() / ".email_game" / "keys"
        if not key_dir.exists():
            return False
        if key_dir.is_file():
            return True
        try:
            return any(item.is_file() for item in key_dir.iterdir())
        except OSError:
            return False

    def _operational_readiness_score(
        self,
        agent_running: bool,
        monitor_running: bool,
        identity_key_present: bool,
        log_stale: bool,
        leaderboard_reachable: bool,
        analysis: object,
    ) -> Tuple[int, str]:
        score = 100
        reasons: List[str] = []
        if not agent_running:
            score -= 30
            reasons.append("agent not running")
        if not monitor_running:
            score -= 20
            reasons.append("monitor not running")
        if not identity_key_present:
            score -= 25
            reasons.append("identity key missing")
        if log_stale:
            score -= 15
            reasons.append("log stale")
        if not leaderboard_reachable:
            score -= 10
            reasons.append("leaderboard not reachable")
        elif getattr(analysis, "rank", None) is None:
            score -= 5
            reasons.append("rank not visible")
        elif getattr(analysis, "score", None) is None:
            score -= 5
            reasons.append("score not visible")
        score = max(0, min(100, score))
        if not reasons:
            return score, "Core runtime, monitor, key, log, and leaderboard checks look ready."
        return score, "; ".join(reasons)

    def _performance_readiness_score(
        self,
        analysis: object,
        recent_reminders: int,
        recent_submissions: int,
        recent_signed_replies: int,
    ) -> Tuple[int, str]:
        score = 100
        reasons: List[str] = []
        deltas = getattr(analysis, "deltas", {})
        if not isinstance(deltas, dict):
            deltas = {}
        known_deltas = [delta for delta in (deltas.get(15), deltas.get(30), deltas.get(60)) if delta is not None]
        if known_deltas and all(int(delta) == 0 for delta in known_deltas):
            score -= 5
            reasons.append("score trend flat recently")
        elif any(int(delta) < 0 for delta in known_deltas):
            score -= 10
            reasons.append("score trend declined recently")
        if recent_reminders > 0:
            score -= min(10, 3 + (recent_reminders * 2))
            reasons.append("action reminders still present")
        missed_submissions = max(0, recent_signed_replies - recent_submissions)
        if missed_submissions > 0:
            score -= min(10, 3 + (missed_submissions * 2))
            reasons.append(f"{missed_submissions} signed replies lack matching submissions")
        recommendation = str(getattr(analysis, "recommendation_title", "") or "")
        if recommendation.lower().startswith("high:"):
            score -= 10
            reasons.append("coach has a high-priority recommendation")
        elif recommendation.lower().startswith("medium:"):
            score -= 5
            reasons.append("coach has a medium-priority recommendation")
        weaknesses = getattr(analysis, "weaknesses", [])
        if isinstance(weaknesses, list) and "No recent matches parsed from local logs." in weaknesses:
            score -= 10
            reasons.append("no recent match evidence parsed")
        score = max(0, min(100, score))
        if not reasons:
            return score, "Recent score trend, reminders, submissions, and coach signals look competition-ready."
        return score, "; ".join(reasons)

    def _readiness_text(self) -> str:
        log_status = self._check_log_stream(reconnect=False, refresh_capture=True)
        agent_running = self._agent_process_running()
        monitor_running = _process_running_pattern(r"scripts/monitor_emailgame_telegram.py")
        identity_key_present = self._identity_key_present()
        branch = self._branch_text()
        commit = self._commit_text()
        model = self._configured_model()
        analysis = self._coach().analyze(persist=True)
        recent = analysis.matches[-3:]
        recent_reminders = sum(match.total_reminders() for match in recent)
        recent_submissions = sum(match.total_submissions() for match in recent)
        recent_signed_replies = sum(match.total_signed_replies() for match in recent)
        data, leaderboard_error = self._fetch_leaderboard_data()
        leaderboard_reachable = data is not None
        operational_score, operational_reason = self._operational_readiness_score(
            agent_running,
            monitor_running,
            identity_key_present,
            bool(log_status.stale or analysis.latest_log_stale),
            leaderboard_reachable,
            analysis,
        )
        performance_score, performance_reason = self._performance_readiness_score(
            analysis,
            recent_reminders,
            recent_submissions,
            recent_signed_replies,
        )
        start_sast = COMPETITION_START_ET.astimezone(SAST_TZ)
        end_sast = COMPETITION_END_ET.astimezone(SAST_TZ)
        lines = [
            "✅ <b>Competition Readiness</b>",
            "",
            f"Official window ET: <b>{COMPETITION_START_ET.strftime('%Y-%m-%d %H:%M')}–{COMPETITION_END_ET.strftime('%H:%M %Z')}</b>",
            f"Official window SAST: <b>{start_sast.strftime('%Y-%m-%d %H:%M')}–{end_sast.strftime('%H:%M %Z')}</b>",
            f"Countdown to start: <b>{html_escape(_format_countdown(COMPETITION_START_ET), quote=False)}</b>",
            "",
            f"Agent process running: <b>{'yes' if agent_running else 'no'}</b>",
            f"Monitor running: <b>{'yes' if monitor_running else 'no'}</b>",
            "Coach running/integrated: <b>yes</b>",
            f"Leaderboard reachable: <b>{'yes' if leaderboard_reachable else 'no'}</b>",
            f"Branch: <code>{html_escape(branch, quote=False)}</code>",
            f"Commit: <code>{html_escape(commit, quote=False)}</code>",
            f"Model: <code>{html_escape(model, quote=False)}</code>",
            f"Identity key location check: <b>{'present yes' if identity_key_present else 'present no'}</b>",
            "One-machine rule: <b>run from the Oracle VM only</b>",
            "",
            f"Current rank/score: <b>{'#' + str(analysis.rank) if analysis.rank is not None else 'n/a'} / {analysis.score if analysis.score is not None else 'n/a'}</b>",
            f"15m score trend: <b>{html_escape(_score_delta_text(analysis.deltas.get(15)), quote=False)}</b>",
            f"30m score trend: <b>{html_escape(_score_delta_text(analysis.deltas.get(30)), quote=False)}</b>",
            f"60m score trend: <b>{html_escape(_score_delta_text(analysis.deltas.get(60)), quote=False)}</b>",
            f"Gap to #4: <b>{analysis.gap_to_four if analysis.gap_to_four is not None else 'n/a'}</b>",
            f"Gap to #1: <b>{analysis.gap_to_one if analysis.gap_to_one is not None else 'n/a'}</b>",
            "",
            f"Recent reminders: <b>{html_escape(str(recent_reminders), quote=False)}</b>",
            f"Recent submissions: <b>{html_escape(str(recent_submissions), quote=False)}</b>",
            f"Recent signed replies: <b>{html_escape(str(recent_signed_replies), quote=False)}</b>",
            f"Log stale: <b>{'yes' if log_status.stale or analysis.latest_log_stale else 'no'}</b>",
            f"Latest coach recommendation: <b>{html_escape(analysis.recommendation_title, quote=False)}</b>",
            "",
            f"Operational readiness: <b>{operational_score}/100</b>",
            html_escape(operational_reason, quote=False),
            "",
            f"Competition performance readiness: <b>{performance_score}/100</b>",
            html_escape(performance_reason, quote=False),
        ]
        if not leaderboard_reachable and leaderboard_error:
            lines.append(f"Leaderboard error: <code>{html_escape(_clean_log_text(leaderboard_error), quote=False)}</code>")
        return "\n".join(lines)

    def _fetch_leaderboard_snapshot(self) -> Tuple[Optional[Dict[str, object]], str]:
        data, error = self._fetch_leaderboard_data()
        if data is None:
            return None, error
        entries = self._leaderboard_entries(data)

        top5 = [entry for entry in entries[:5] if isinstance(entry, dict)]
        me = self._my_leaderboard_entry(entries)
        if not isinstance(me, dict):
            me = {}

        rank_raw = me.get("rank")
        score_raw = me.get("elo")
        rank = int(rank_raw) if str(rank_raw).strip().isdigit() else None
        score = int(score_raw) if str(score_raw).strip().lstrip("-").isdigit() else None
        gap_to_four = self._leaderboard_gap_to_rank(entries, 4, me if me else None)
        gap_to_one = self._leaderboard_gap_to_rank(entries, 1, me if me else None)
        gap_four_val = int(gap_to_four) if gap_to_four.isdigit() else None
        gap_one_val = int(gap_to_one) if gap_to_one.isdigit() else None
        in_top5 = bool(rank is not None and rank <= 5)

        snapshot: Dict[str, object] = {
            "fetched_at": _now_sast().isoformat(),
            "rank": rank,
            "score": score,
            "gap_to_four": gap_four_val,
            "gap_to_one": gap_one_val,
            "in_top5": in_top5,
            "top5": [
                {
                    "rank": entry.get("rank"),
                    "agent_id": str(entry.get("agent_id", "")),
                    "elo": entry.get("elo"),
                }
                for entry in top5
            ],
        }
        return snapshot, ""

    def _leaderboard_snapshot_changed(
        self,
        previous: Dict[str, object],
        current: Dict[str, object],
    ) -> bool:
        keys = ("rank", "score", "gap_to_four", "gap_to_one", "in_top5")
        return any(previous.get(key) != current.get(key) for key in keys)

    def _render_leaderboard_top5(self, snapshot: Dict[str, object]) -> List[str]:
        lines = []
        top5 = snapshot.get("top5")
        if not isinstance(top5, list):
            return ["Top 5: unavailable"]
        lines.append("Top 5:")
        for entry in top5:
            if not isinstance(entry, dict):
                continue
            rank = entry.get("rank", "?")
            agent_id = self._leaderboard_agent_label(str(entry.get("agent_id") or "unknown"))
            score = entry.get("elo", "?")
            lines.append(f"{rank}. {agent_id} — {score}")
        return lines

    def _leaderboard_change_message(
        self,
        previous: Dict[str, object],
        current: Dict[str, object],
    ) -> Tuple[bool, str]:
        if not self._leaderboard_snapshot_changed(previous, current):
            return False, ""

        current_rank = current.get("rank")
        current_score = current.get("score")
        previous_rank = previous.get("rank")
        previous_score = previous.get("score")
        previous_gap_four = previous.get("gap_to_four")
        current_gap_four = current.get("gap_to_four")
        previous_gap_one = previous.get("gap_to_one")
        current_gap_one = current.get("gap_to_one")

        lines = [
            f"{html_escape(_format_sast(), quote=False)} • Leaderboard update",
            "",
            "🏆 <b>Email Game Leaderboard Update</b>",
            "",
        ]

        if previous_rank is not None and previous_rank != current_rank:
            lines.append(f"Rank: #{html_escape(str(previous_rank), quote=False)} → #{html_escape(str(current_rank), quote=False)}")
        elif current_rank is not None:
            lines.append(f"Rank: #{html_escape(str(current_rank), quote=False)}")
        else:
            lines.append("Rank: n/a")

        if previous_score is not None and current_score is not None:
            delta = current_score - previous_score
            sign = "+" if delta >= 0 else ""
            lines.append(
                f"Score: {html_escape(str(previous_score), quote=False)} → {html_escape(str(current_score), quote=False)} "
                f"({sign}{html_escape(str(delta), quote=False)})"
            )
        elif current_score is not None:
            lines.append(f"Score: {html_escape(str(current_score), quote=False)}")
        else:
            lines.append("Score: n/a")

        if previous_gap_four is not None and current_gap_four is not None:
            lines.append(
                f"Gap to #4: {html_escape(str(previous_gap_four), quote=False)} → {html_escape(str(current_gap_four), quote=False)}"
            )
        elif current_gap_four is not None:
            lines.append(f"Gap to #4: {html_escape(str(current_gap_four), quote=False)}")
        else:
            lines.append("Gap to #4: n/a")

        if previous_gap_one is not None and current_gap_one is not None:
            lines.append(
                f"Gap to #1: {html_escape(str(previous_gap_one), quote=False)} → {html_escape(str(current_gap_one), quote=False)}"
            )
        elif current_gap_one is not None:
            lines.append(f"Gap to #1: {html_escape(str(current_gap_one), quote=False)}")
        else:
            lines.append("Gap to #1: n/a")

        lines.append("")
        lines.extend(self._render_leaderboard_top5(current))
        return True, "\n".join(lines)

    def _version_text(self) -> str:
        branch = self._branch_text()
        commit = self._commit_text()
        model = self._configured_model()
        return (
            "🧩 <b>Email Game Version</b>\n\n"
            f"Branch: <code>{html_escape(branch, quote=False)}</code>\n"
            f"Commit: <code>{html_escape(commit, quote=False)}</code>\n"
            f"Agent: <code>{html_escape(self._agent_name, quote=False)}</code>\n"
            f"Model: <code>{html_escape(model, quote=False)}</code>\n"
            f"Script: <code>{html_escape(Path(__file__).name, quote=False)}</code>"
        )

    def _preflight_text(self) -> str:
        script = PROJECT_ROOT / "preflight_papzin_agent.sh"
        if not script.exists():
            return "Preflight script not found."
        try:
            result = _run_command(
                [
                    "bash",
                    "-lc",
                    "cd /home/ubuntu/hackathons/theemailgame && "
                    "./.venv/bin/python -m py_compile my_agent.py && "
                    "bash -n run_papzin_agent.sh && "
                    "bash -n preflight_papzin_agent.sh && "
                    "bash preflight_papzin_agent.sh --check-key",
                ],
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "Preflight timed out."
        output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part.strip())
        output = _redact(output)
        output = re.sub(r"(?im)^\s*key:\s*[^\r\n]+$", "key: configured", output)
        output = re.sub(r"(?im)^\s*key:\s*\[redacted\]$", "key: configured", output)
        if result.returncode == 0:
            return "Preflight passed." if not output else "Preflight passed.\n" + html_escape(_clean_log_text(output), quote=False)
        if not output:
            return f"Preflight failed with exit code {result.returncode}."
        return f"Preflight failed with exit code {result.returncode}.\n{html_escape(_clean_log_text(output), quote=False)}"

    def _start_agent_text(self) -> str:
        if _process_running_pattern(r"scripts/run_custom_agent.py letlhogonolo_fanampe"):
            return "Agent already running."
        if self._match_active():
            return "Refusing to start while a match is active."
        result = _tmux_launch_agent_in_session() if _tmux_has_session(TMUX_SESSION_AGENT) else _tmux_start_agent_session()
        if result.returncode == 0:
            return "Agent start requested."
        return f"Failed to start agent: {html_escape(_clean_log_text((result.stderr or result.stdout).strip() or 'unknown error'), quote=False)}"

    def _stop_agent_text(self) -> str:
        phase = self._derive_phase()
        if phase == "in game":
            return "Agent is IN GAME. I will not stop because it may forfeit the match."
        if not _process_running_pattern(r"scripts/run_custom_agent.py letlhogonolo_fanampe"):
            return "Agent session is not running."
        result = _tmux_send_ctrl_c(TMUX_SESSION_AGENT)
        if result.returncode == 0:
            return "Agent stop requested."
        return f"Failed to stop agent: {html_escape(_clean_log_text((result.stderr or result.stdout).strip() or 'unknown error'), quote=False)}"

    def _restart_agent_text(self) -> str:
        phase = self._derive_phase()
        if phase == "in game":
            return "Agent is IN GAME. I will not restart because it may forfeit the match."
        if phase not in ("between matches", "disconnected", "crashed"):
            return "Restart refused until the log shows Game over - between matches now, disconnected, or crashed."
        if not _process_running_pattern(r"scripts/run_custom_agent.py letlhogonolo_fanampe"):
            return self._start_agent_text()
        stop = _tmux_send_ctrl_c(TMUX_SESSION_AGENT)
        if stop.returncode != 0:
            return f"Failed to stop agent before restart: {html_escape(_clean_log_text((stop.stderr or stop.stdout).strip() or 'unknown error'), quote=False)}"
        time.sleep(1.0)
        start = _tmux_launch_agent_in_session() if _tmux_has_session(TMUX_SESSION_AGENT) else _tmux_start_agent_session()
        if start.returncode == 0:
            return "Agent restart requested."
        return f"Failed to restart agent: {html_escape(_clean_log_text((start.stderr or start.stdout).strip() or 'unknown error'), quote=False)}"

    def _branch_text(self) -> str:
        result = _run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "unknown"

    def _commit_text(self) -> str:
        result = _run_command(["git", "rev-parse", "--short", "HEAD"], timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "unknown"

    def _state_card(self, agent_running: bool, phase: str) -> Tuple[str, str]:
        if phase == "in game":
            return ("IN GAME", "🟢")
        if phase == "between matches":
            return ("Between matches", "⚪")
        if phase == "queued":
            return ("Waiting", "🟡")
        if phase in ("disconnected", "crashed"):
            return (phase.replace("_", " ").title(), "🔴")
        if not agent_running:
            return ("Stopped", "🔴")
        return ("Waiting", "🟡")

    def _is_match_start(self, line: str) -> bool:
        lower = line.lower()
        return "match found - game starting!" in lower or "🎮 in game - round 1" in lower

    def _is_match_end(self, line: str) -> bool:
        return "game over - between matches now" in line.lower()

    def _leaderboard_agent_label(self, agent_id: str) -> str:
        label = agent_id
        if agent_id == self._agent_name:
            label += " ⭐"
        elif agent_id in HOUSE_BOT_IDS:
            label += " 🤖"
        return label

    def _leaderboard_gap_to_rank(
        self,
        entries: List[Dict[str, object]],
        target_rank: int,
        me: Optional[Dict[str, object]],
    ) -> str:
        if not isinstance(me, dict):
            return "n/a"
        target = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict)
                and str(entry.get("rank") or "").strip().isdigit()
                and int(str(entry.get("rank")).strip()) == target_rank
            ),
            None,
        )
        if not isinstance(target, dict):
            return "n/a"
        try:
            target_score = int(target.get("elo"))
            my_score = int(me.get("elo"))
        except Exception:
            return "n/a"
        return str(max(target_score - my_score, 0))

    def _sorted_round_summaries(self, summary: MatchSummary) -> Dict[str, RoundSummary]:
        return dict(
            sorted(
                summary.rounds.items(),
                key=lambda item: (0, int(item[0])) if item[0].isdigit() else (1, item[0]),
            )
        )

    def _append_unique(self, values: List[str], value: str) -> None:
        if value and value not in values:
            values.append(value)

    def _round_entry(self, summary: MatchSummary, round_id: str, ts: datetime) -> RoundSummary:
        round_summary = summary.rounds.get(round_id)
        if round_summary is None:
            round_summary = RoundSummary(round_id=round_id, started_at=ts)
            summary.rounds[round_id] = round_summary
        elif ts < round_summary.started_at:
            round_summary.started_at = ts
        return round_summary

    def _submission_round_entry(
        self,
        summary: MatchSummary,
        current_round_id: Optional[str],
        agent: str,
        ts: datetime,
    ) -> Optional[RoundSummary]:
        if current_round_id:
            return self._round_entry(summary, current_round_id, ts)

        for round_summary in reversed(list(self._sorted_round_summaries(summary).values())):
            if agent in round_summary.signed_from:
                return round_summary

        sorted_rounds = list(self._sorted_round_summaries(summary).values())
        if sorted_rounds:
            return sorted_rounds[-1]
        return None

    def _extract_round_id(self, line: str) -> Optional[str]:
        patterns = (
            r"IN GAME - Round\s+(\d+)",
            r"\[INFO\]\s*Round\s+(\d+):",
            r"Round\s+(\d+)\b.*Instructions",
            r"\(Round\s+(\d+)\)\s*$",
        )
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _latest_match_summary(self) -> Optional[MatchSummary]:
        structured_entries = self._structured_event_entries()
        entries = structured_entries or self._recent_observed_entries(MAX_OBSERVED_LINES)
        if not entries:
            return None

        def is_match_activity(line: str) -> bool:
            log_like = line.startswith("[INFO]") or line.startswith(f"[{self._agent_name}]")
            return bool(
                self._is_match_start(line)
                or self._is_match_end(line)
                or (log_like and self._extract_round_id(line))
                or "Sent signature request to " in line
                or "Submitted received signature from " in line
                or re.search(r"received from [^:]+:", line, re.IGNORECASE)
            )

        latest_activity_index = None
        for idx, entry in enumerate(entries):
            if is_match_activity(entry.text):
                latest_activity_index = idx
        if latest_activity_index is None:
            return None

        previous_end_index = None
        for idx in range(latest_activity_index - 1, -1, -1):
            if self._is_match_end(entries[idx].text):
                previous_end_index = idx
                break
        segment_start = previous_end_index + 1 if previous_end_index is not None else 0

        start_index = None
        for idx in range(segment_start, latest_activity_index + 1):
            if self._is_match_start(entries[idx].text):
                start_index = idx
        if start_index is None:
            for idx in range(segment_start, latest_activity_index + 1):
                if self._extract_round_id(entries[idx].text) or re.search(
                    r"\[INFO\]\s*Round\s+1:", entries[idx].text, re.IGNORECASE
                ):
                    start_index = idx
                    break
        if start_index is None:
            start_index = max(segment_start, latest_activity_index - MAX_LOG_LINES + 1)

        end_index = None
        for idx in range(start_index, len(entries)):
            if self._is_match_end(entries[idx].text):
                end_index = idx
        window = entries[start_index : end_index + 1 if end_index is not None else len(entries)]
        if not window:
            return None

        summary = MatchSummary(started_at=window[0].ts, source_count=len(window))
        current_round_id: Optional[str] = None
        for entry in window:
            line = _clean_log_text(entry.text)
            if not line:
                continue

            if self._is_match_start(line):
                summary.started_at = entry.ts
                continue

            round_id = self._extract_round_id(line)
            if round_id:
                current_round_id = round_id
                self._round_entry(summary, round_id, entry.ts)

            round_targets_match = re.search(r"\[INFO\]\s*Round\s+(\d+):.*request_targets=(\d+)", line, re.IGNORECASE)
            if round_targets_match:
                round_summary = self._round_entry(summary, round_targets_match.group(1), entry.ts)
                round_summary.request_targets = int(round_targets_match.group(2))

            if "Sent signature request to " in line:
                agent = line.rsplit("Sent signature request to ", 1)[-1].strip()
                if current_round_id:
                    round_summary = self._round_entry(summary, current_round_id, entry.ts)
                    self._append_unique(round_summary.requested_from, agent)
                continue

            request_match = re.search(
                r"received from ([^:]+): .*?(?:Request for signature|Signature Request)(?! Response| Declined| Decline| Response).*?\(Round (\d+)\)",
                line,
                re.IGNORECASE,
            )
            if request_match:
                agent = request_match.group(1).strip()
                round_id = request_match.group(2)
                round_summary = self._round_entry(summary, round_id, entry.ts)
                self._append_unique(round_summary.received_from, agent)
                continue

            signed_match = re.search(r"received from ([^:]+): Signed Message \(Round (\d+)\)", line, re.IGNORECASE)
            if signed_match:
                agent = signed_match.group(1).strip()
                round_id = signed_match.group(2)
                round_summary = self._round_entry(summary, round_id, entry.ts)
                self._append_unique(round_summary.signed_from, agent)
                continue

            submit_match = re.search(r"Submitted received signature from ([^ ]+)", line, re.IGNORECASE)
            if submit_match:
                agent = submit_match.group(1).strip()
                round_summary = self._submission_round_entry(summary, current_round_id, agent, entry.ts)
                if round_summary is not None:
                    round_summary.submitted = True
                    self._append_unique(round_summary.signed_from, agent)
                continue

            submit_round_match = re.search(r"submitted signature for round (\d+) from ([^ ]+)", line, re.IGNORECASE)
            if submit_round_match:
                round_summary = self._round_entry(summary, submit_round_match.group(1), entry.ts)
                round_summary.submitted = True
                self._append_unique(round_summary.signed_from, submit_round_match.group(2).strip())
                continue

            submitting_match = re.search(r"Submitting signed payload from ([^ ]+)", line, re.IGNORECASE)
            if submitting_match:
                agent = submitting_match.group(1).strip()
                round_summary = self._submission_round_entry(summary, current_round_id, agent, entry.ts)
                if round_summary is not None:
                    round_summary.submitted = True
                    self._append_unique(round_summary.signed_from, agent)
                continue

            sign_request_match = re.search(r"Signed request from ([^ ]+)", line, re.IGNORECASE)
            if sign_request_match and current_round_id:
                round_summary = self._round_entry(summary, current_round_id, entry.ts)
                round_summary.submitted = True
                continue

            reminder_match = re.search(
                r"received from system_reminder: .*Action Completion Reminder \(Round (\d+)\)",
                line,
                re.IGNORECASE,
            )
            if reminder_match:
                round_id = reminder_match.group(1)
                round_summary = self._round_entry(summary, round_id, entry.ts)
                round_summary.reminders += 1
                continue

            if self._is_match_end(line):
                summary.ended = True
                summary.ended_at = entry.ts
                continue

        if not summary.rounds and not summary.ended:
            return None
        return summary

    def _format_agent_list(self, agents: List[str]) -> str:
        if not agents:
            return "none"
        return ", ".join(html_escape(self._display_agent_label(agent), quote=False) for agent in agents)

    def _format_requested_agents(self, round_summary: RoundSummary) -> str:
        if round_summary.requested_from:
            return self._format_agent_list(round_summary.requested_from)
        if round_summary.request_targets:
            target_count = html_escape(str(round_summary.request_targets), quote=False)
            suffix = "target" if round_summary.request_targets == 1 else "targets"
            return f"not captured in current tail (expected {target_count} {suffix})"
        return "not observed"

    def _display_agent_label(self, agent_id: str) -> str:
        if agent_id in HOUSE_BOT_IDS:
            return f"{agent_id} (house bot)"
        return agent_id

    def _format_yes_no_unknown(self, value: Optional[bool]) -> str:
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return "unknown"

    def _round_summary_lines(self, round_summary: RoundSummary) -> List[str]:
        lines = [f"<b>Round {html_escape(round_summary.round_id, quote=False)}</b>"]
        lines.append(f"📨 We requested signatures from: {self._format_requested_agents(round_summary)}")
        lines.append(f"📥 Requests received from: {self._format_agent_list(round_summary.received_from)}")
        lines.append(f"✅ Signed replies received: {self._format_agent_list(round_summary.signed_from)}")
        lines.append(f"📤 Submitted to moderator: {self._format_yes_no_unknown(round_summary.submitted)}")
        lines.append(f"⚠️ Reminder: {'yes' if round_summary.reminders > 0 else 'no'}")
        return lines

    def _match_summary_line(self, summary: Optional[MatchSummary]) -> str:
        if summary is None:
            return "No match summary available yet."
        rounds = self._sorted_round_summaries(summary)
        round_ids = list(rounds.keys())
        if not round_ids:
            return f"{_format_sast(summary.started_at)} • Match started"
        last_round = round_ids[-1]
        reminder_count = sum(round_summary.reminders for round_summary in rounds.values())
        status_bits = [
            f"Round {last_round}",
            "ended" if summary.ended else "in progress",
            f"reminders {reminder_count}",
        ]
        return f"{_format_sast(summary.started_at)} • Match started; " + ", ".join(status_bits)

    def _render_match_summary(
        self,
        summary: MatchSummary,
        title: str,
        include_note: bool,
        compact_reminders: bool,
        idle_note: bool = False,
    ) -> str:
        lines = [title, ""]
        lines.append(f"Latest completed match: <b>{html_escape(_format_sast(summary.started_at), quote=False)}</b>")
        if idle_note:
            lines.append("No newer match activity observed yet.")
        lines.append(f"{_format_sast(summary.started_at)} • Match started")
        lines.append("")

        rounds = self._sorted_round_summaries(summary)
        reminder_round_count = 0
        for round_id, round_summary in rounds.items():
            lines.append(f"<b>Round {html_escape(round_id, quote=False)}</b>")
            lines.append(f"📨 We requested signatures from: {self._format_requested_agents(round_summary)}")
            lines.append(f"📥 Requests received from: {self._format_agent_list(round_summary.received_from)}")
            lines.append(f"✅ Signed replies received: {self._format_agent_list(round_summary.signed_from)}")
            lines.append(f"📤 Submitted to moderator: {self._format_yes_no_unknown(round_summary.submitted)}")
            if compact_reminders:
                if round_summary.reminders > 0:
                    reminder_round_count += 1
            else:
                lines.append(f"⚠️ Reminder: {'yes' if round_summary.reminders > 0 else 'no'}")
            lines.append("")

        if compact_reminders:
            if reminder_round_count > 0:
                lines.append(f"⚠️ Action reminders: {reminder_round_count} recent rounds")
            else:
                lines.append("⚠️ Action reminders: none observed")
        lines.append("🏁 Game ended" if summary.ended else "🏁 Game in progress")
        if include_note:
            lines.extend(
                [
                    "",
                    "Note: house_bot_* are built-in competition bots/opponents.",
                ]
            )
        return "\n".join(lines)

    def _configured_model(self) -> str:
        model = _env("OPENAI_MODEL")
        if model in {"gpt-4.1", "gpt-4.1-mini"}:
            return model
        return "gpt-4.1-mini"


def main() -> int:
    monitor = EmailGameMonitor(LOG_FILE, STATE_FILE)
    return monitor.run()


if __name__ == "__main__":
    raise SystemExit(main())
