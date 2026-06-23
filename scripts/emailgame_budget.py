#!/usr/bin/env python3
"""Passive LLM budget and usage analysis for the Email Game agent.

This module reads local logs/state only. It does not call the LLM gateway.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-live.log"
MONITOR_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-monitor-state.json"
LEADERBOARD_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-leaderboard-state.json"
COACH_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-coach-state.json"
BUDGET_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-budget-state.json"

SAST_TZ = ZoneInfo("Africa/Johannesburg") if ZoneInfo is not None else timezone(timedelta(hours=2))
CONFIGURED_BUDGET_USD = 30
DEFAULT_MODEL = "gpt-4.1-mini"
MAX_LOG_BYTES = 2_500_000
ALERT_COOLDOWN_SECONDS = 15 * 60
LLM_SPIKE_15M_THRESHOLD = 20
FLAT_USAGE_15M_THRESHOLD = 6

CALL_RE = re.compile(
    r"HTTP Request:\s+POST\s+https://the-email-game-llm\.fly\.dev/(?:v1/)?chat/completions",
    re.IGNORECASE,
)
MODEL_RE = re.compile(r"\bmodel=([A-Za-z0-9_.:-]+)")
TOKEN_FIELD_RE = re.compile(r"\b(prompt_tokens|completion_tokens|total_tokens)\b[\"']?\s*[:=]\s*(\d+)", re.IGNORECASE)
TIME_PREFIX_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")


def _now() -> datetime:
    return datetime.now(tz=SAST_TZ)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SAST_TZ)
    return parsed.astimezone(SAST_TZ)


def _fmt_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "n/a"
    return value.astimezone(SAST_TZ).strftime("%H:%M SAST")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _score_delta_text(delta: Optional[int]) -> str:
    if delta is None:
        return "n/a"
    return f"{delta:+d}"


def _count_since(calls: Iterable[datetime], now: datetime, minutes: int) -> int:
    cutoff = now - timedelta(minutes=minutes)
    return sum(1 for call_at in calls if call_at >= cutoff)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


@dataclass
class BudgetAnalysis:
    budget_usd: int
    exact_spend_available: bool
    estimated_spend_available: bool
    token_tracking_available: bool
    total_calls: int
    calls_15m: int
    calls_30m: int
    calls_60m: int
    latest_call_at: Optional[datetime]
    model: str
    gateway_label: str
    score_deltas: Dict[int, Optional[int]]
    token_totals: Dict[str, int] = field(default_factory=dict)
    parse_errors: int = 0
    warnings: List[str] = field(default_factory=list)


class EmailGameBudget:
    def __init__(
        self,
        log_file: Path = LOG_FILE,
        monitor_state_file: Path = MONITOR_STATE_FILE,
        leaderboard_state_file: Path = LEADERBOARD_STATE_FILE,
        coach_state_file: Path = COACH_STATE_FILE,
        budget_state_file: Path = BUDGET_STATE_FILE,
    ) -> None:
        self.log_file = log_file
        self.monitor_state_file = monitor_state_file
        self.leaderboard_state_file = leaderboard_state_file
        self.coach_state_file = coach_state_file
        self.budget_state_file = budget_state_file
        self.state = _read_json(budget_state_file)

    def analyze(self, *, persist: bool = True) -> BudgetAnalysis:
        now = _now()
        log_text = self._read_recent_log_text()
        log_calls, untimed_calls, token_totals, parse_errors, model = self._parse_log(log_text, now)
        observed_calls = self._parse_observed_calls()
        calls_for_windows = observed_calls or log_calls
        score_deltas = self._score_deltas()
        token_tracking_available = any(token_totals.values())
        windows_available = bool(calls_for_windows)
        calls_15m = _count_since(calls_for_windows, now, 15) if windows_available else 0
        calls_30m = _count_since(calls_for_windows, now, 30) if windows_available else 0
        calls_60m = _count_since(calls_for_windows, now, 60) if windows_available else 0
        total_calls = len(log_calls) + untimed_calls
        warnings = self._warnings(calls_15m, score_deltas, token_tracking_available, parse_errors, windows_available)
        analysis = BudgetAnalysis(
            budget_usd=CONFIGURED_BUDGET_USD,
            exact_spend_available=False,
            estimated_spend_available=False,
            token_tracking_available=token_tracking_available,
            total_calls=total_calls,
            calls_15m=calls_15m,
            calls_30m=calls_30m,
            calls_60m=calls_60m,
            latest_call_at=max(calls_for_windows) if calls_for_windows else None,
            model=model or DEFAULT_MODEL,
            gateway_label="hackathon gateway",
            score_deltas=score_deltas,
            token_totals=token_totals,
            parse_errors=parse_errors,
            warnings=warnings,
        )
        if persist:
            self._record_state(analysis, now)
        return analysis

    def budget_text(self) -> str:
        analysis = self.analyze(persist=True)
        lines = [
            "💰 <b>Email Game Budget</b>",
            "",
            f"Budget: <b>${analysis.budget_usd}</b>",
            "Exact spend: <b>unavailable from gateway/logs</b>",
            "Estimated spend: <b>unknown</b>",
            "Remaining: <b>unknown</b>",
            "",
            "<b>LLM calls</b>:",
            f"15m: <b>{self._window_text(analysis.calls_15m, analysis.latest_call_at)}</b>",
            f"30m: <b>{self._window_text(analysis.calls_30m, analysis.latest_call_at)}</b>",
            f"60m: <b>{self._window_text(analysis.calls_60m, analysis.latest_call_at)}</b>",
            f"Total observed: <b>{analysis.total_calls}</b>",
            "",
            f"Model: <code>{html_escape(analysis.model, quote=False)}</code>",
            f"Gateway: <b>{analysis.gateway_label}</b>",
            f"Exact token/cost data available: <b>{'yes' if analysis.token_tracking_available else 'no'}</b>",
            "",
            "Note: exact credit balance is unavailable unless the gateway exposes usage/cost data.",
        ]
        if analysis.warnings:
            lines.extend(["", "<b>Warnings</b>:"])
            lines.extend(f"- {html_escape(item, quote=False)}" for item in analysis.warnings[:4])
        return "\n".join(lines)

    def usage_text(self) -> str:
        analysis = self.analyze(persist=True)
        score_per_call_15m = "n/a"
        delta_15 = analysis.score_deltas.get(15)
        if delta_15 is not None and analysis.calls_15m:
            score_per_call_15m = f"{delta_15 / analysis.calls_15m:.2f}"
        lines = [
            "📊 <b>Email Game LLM Usage</b>",
            "",
            f"Latest call: <b>{html_escape(_fmt_dt(analysis.latest_call_at), quote=False)}</b>",
            f"Calls 15m: <b>{self._window_text(analysis.calls_15m, analysis.latest_call_at)}</b>",
            f"Calls 30m: <b>{self._window_text(analysis.calls_30m, analysis.latest_call_at)}</b>",
            f"Calls 60m: <b>{self._window_text(analysis.calls_60m, analysis.latest_call_at)}</b>",
            f"Total observed: <b>{analysis.total_calls}</b>",
            "",
            "<b>Score trend</b>:",
            f"15m: <b>{html_escape(_score_delta_text(analysis.score_deltas.get(15)), quote=False)}</b>",
            f"30m: <b>{html_escape(_score_delta_text(analysis.score_deltas.get(30)), quote=False)}</b>",
            f"60m: <b>{html_escape(_score_delta_text(analysis.score_deltas.get(60)), quote=False)}</b>",
            "",
            "<b>Token usage fields parsed</b>:",
            f"prompt_tokens: <b>{analysis.token_totals.get('prompt_tokens', 0)}</b>",
            f"completion_tokens: <b>{analysis.token_totals.get('completion_tokens', 0)}</b>",
            f"total_tokens: <b>{analysis.token_totals.get('total_tokens', 0)}</b>",
            "",
            "<b>Efficiency</b>:",
            f"Score per 15m call: <b>{html_escape(score_per_call_15m, quote=False)}</b> estimate only",
        ]
        if not analysis.token_tracking_available:
            lines.append("Exact token/cost data is not available in current logs.")
        if analysis.latest_call_at is None and analysis.total_calls:
            lines.append("Recent usage windows are unavailable because current HTTP call log lines have no timestamps.")
        if analysis.warnings:
            lines.extend(["", "<b>Warnings</b>:"])
            lines.extend(f"- {html_escape(item, quote=False)}" for item in analysis.warnings[:4])
        return "\n".join(lines)

    def maybe_alert_text(self) -> Optional[str]:
        now = _now()
        analysis = self.analyze(persist=True)
        alert_warnings = self._alertable_warnings(analysis.warnings)
        if not alert_warnings:
            self.state["consecutive_parse_failures"] = 0 if analysis.parse_errors == 0 else int(self.state.get("consecutive_parse_failures") or 0)
            _write_json(self.budget_state_file, self.state)
            return None
        key = "|".join(alert_warnings)
        last = self.state.get("last_alert")
        if isinstance(last, dict):
            last_key = str(last.get("key") or "")
            last_at = float(last.get("at") or 0)
            if last_key == key and time.time() - last_at < ALERT_COOLDOWN_SECONDS:
                _write_json(self.budget_state_file, self.state)
                return None
        self.state["last_alert"] = {"key": key, "at": time.time(), "warnings": alert_warnings}
        _write_json(self.budget_state_file, self.state)
        lines = [
            f"{html_escape(_fmt_dt(now), quote=False)} • ⚠️ <b>Email Game Budget Alert</b>",
            "",
            f"Calls 15m: <b>{analysis.calls_15m}</b>",
            f"Score 15m: <b>{html_escape(_score_delta_text(analysis.score_deltas.get(15)), quote=False)}</b>",
            "",
        ]
        lines.extend(f"- {html_escape(item, quote=False)}" for item in alert_warnings[:4])
        return "\n".join(lines)

    def _alertable_warnings(self, warnings: List[str]) -> List[str]:
        alertable_markers = (
            "spiked heavily",
            "score trend is flat/down",
            "parsing failed repeatedly",
            "budget remaining",
        )
        return [
            warning
            for warning in warnings
            if any(marker in warning.lower() for marker in alertable_markers)
        ]

    def _read_recent_log_text(self) -> str:
        if not self.log_file.exists():
            return ""
        try:
            with self.log_file.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - MAX_LOG_BYTES))
                return handle.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def _window_text(self, value: int, latest_call_at: Optional[datetime]) -> str:
        if latest_call_at is None:
            return "unavailable"
        return str(value)

    def _parse_log(self, text: str, now: datetime) -> Tuple[List[datetime], int, Dict[str, int], int, str]:
        calls: List[datetime] = []
        untimed_calls = 0
        token_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        parse_errors = 0
        model = ""
        current_day = now.date()
        for line in text.splitlines():
            model_match = MODEL_RE.search(line)
            if model_match:
                candidate = model_match.group(1)
                if candidate in {"gpt-4.1", "gpt-4.1-mini"}:
                    model = candidate
            for token_name, token_value in TOKEN_FIELD_RE.findall(line):
                try:
                    token_totals[token_name.lower()] += int(token_value)
                except ValueError:
                    parse_errors += 1
            if not CALL_RE.search(line):
                continue
            call_at = self._line_timestamp(line, now, current_day)
            if call_at is None:
                untimed_calls += 1
            else:
                calls.append(call_at)
        calls.sort()
        return calls, untimed_calls, token_totals, parse_errors, model

    def _parse_observed_calls(self) -> List[datetime]:
        state = _read_json(self.monitor_state_file)
        observed = state.get("observed_lines")
        if not isinstance(observed, list):
            return []
        calls: List[datetime] = []
        for item in observed:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            if not CALL_RE.search(text):
                continue
            ts = _parse_dt(item.get("ts"))
            if ts is not None:
                calls.append(ts)
        calls.sort()
        return calls

    def _line_timestamp(self, line: str, now: datetime, current_day: Any) -> Optional[datetime]:
        match = TIME_PREFIX_RE.search(line)
        if not match:
            return None
        try:
            hour, minute, second = [int(part) for part in match.group(1).split(":")]
        except ValueError:
            return None
        candidate = datetime(
            current_day.year,
            current_day.month,
            current_day.day,
            hour,
            minute,
            second,
            tzinfo=SAST_TZ,
        )
        if candidate > now + timedelta(minutes=5):
            candidate -= timedelta(days=1)
        return candidate

    def _score_deltas(self) -> Dict[int, Optional[int]]:
        state = _read_json(self.coach_state_file)
        history = state.get("leaderboard_history")
        if not isinstance(history, list):
            return {15: None, 30: None, 60: None}
        items = [item for item in history if isinstance(item, dict)]
        current = None
        for item in reversed(items):
            if _int_or_none(item.get("score")) is not None:
                current = item
                break
        if current is None:
            return {15: None, 30: None, 60: None}
        current_time = _parse_dt(current.get("fetched_at"))
        current_score = _int_or_none(current.get("score"))
        if current_time is None or current_score is None:
            return {15: None, 30: None, 60: None}
        deltas: Dict[int, Optional[int]] = {}
        for minutes in (15, 30, 60):
            cutoff = current_time - timedelta(minutes=minutes)
            previous = None
            for item in items:
                item_time = _parse_dt(item.get("fetched_at"))
                if item_time is not None and item_time <= cutoff and _int_or_none(item.get("score")) is not None:
                    previous = item
            previous_score = _int_or_none(previous.get("score")) if previous else None
            deltas[minutes] = current_score - previous_score if previous_score is not None else None
        return deltas

    def _warnings(
        self,
        calls_15m: int,
        score_deltas: Dict[int, Optional[int]],
        token_tracking_available: bool,
        parse_errors: int,
        windows_available: bool,
    ) -> List[str]:
        warnings: List[str] = []
        score_15m = score_deltas.get(15)
        if windows_available and calls_15m >= LLM_SPIKE_15M_THRESHOLD:
            warnings.append("LLM calls spiked heavily in the last 15 minutes.")
        if windows_available and calls_15m >= FLAT_USAGE_15M_THRESHOLD and score_15m is not None and score_15m <= 0:
            warnings.append("Usage increased while 15m score trend is flat/down.")
        if parse_errors >= 5:
            warnings.append("Usage parsing failed repeatedly; call counts may be incomplete.")
        if not token_tracking_available:
            warnings.append("Exact token/cost data is unavailable from current logs.")
        if not windows_available:
            warnings.append("Recent usage windows are unavailable because current LLM call logs have no timestamps.")
        return warnings

    def _record_state(self, analysis: BudgetAnalysis, now: datetime) -> None:
        self.state["last_analysis_at"] = now.isoformat()
        self.state["last_snapshot"] = {
            "budget_usd": analysis.budget_usd,
            "exact_spend_available": analysis.exact_spend_available,
            "estimated_spend_available": analysis.estimated_spend_available,
            "token_tracking_available": analysis.token_tracking_available,
            "total_calls": analysis.total_calls,
            "calls_15m": analysis.calls_15m,
            "calls_30m": analysis.calls_30m,
            "calls_60m": analysis.calls_60m,
            "latest_call_at": analysis.latest_call_at.isoformat() if analysis.latest_call_at else "",
            "model": analysis.model,
            "score_deltas": {str(key): value for key, value in analysis.score_deltas.items()},
            "token_totals": analysis.token_totals,
            "parse_errors": analysis.parse_errors,
            "warnings": analysis.warnings,
        }
        _write_json(self.budget_state_file, self.state)


def main() -> int:
    budget = EmailGameBudget()
    print(budget.budget_text())
    print()
    print(budget.usage_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
