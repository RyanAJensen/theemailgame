#!/usr/bin/env python3
"""Performance coach for the Email Game agent.

The coach reads local redacted-able artifacts, summarizes recent match quality,
and returns recommendations. It does not execute arbitrary commands and does
not change agent behavior.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-live.log"
MONITOR_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-monitor-state.json"
LEADERBOARD_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-leaderboard-state.json"
COACH_STATE_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-coach-state.json"
MATCH_REVIEW_FILE = PROJECT_ROOT / "MATCH_REVIEW.md"

SAST_TZ = ZoneInfo("Africa/Johannesburg") if ZoneInfo is not None else timezone(timedelta(hours=2))
AGENT_NAME_DEFAULT = "letlhogonolo_fanampe"
HOUSE_BOT_IDS = {"house_bot_1", "house_bot_2", "house_bot_3"}
MAX_LOG_BYTES = 1_500_000
MAX_HISTORY = 400
MAX_MATCHES = 12
ALERT_COOLDOWN_SECONDS = 600

WATCH_URL_RE = re.compile(r"https?://(?:www\.)?the-email-game\.fly\.dev/watch\?[^\s<>\"]+", re.IGNORECASE)
TOKEN_KV_RE = re.compile(r"(token=)[^\s&<>\"]+", re.IGNORECASE)
SENSITIVE_KV_RE = re.compile(
    r"(\b(?:OPENAI_API_KEY|EMAIL_GAME_API_KEY|EMAIL_GAME_TELEGRAM_BOT_TOKEN|"
    r"TELEGRAM_BOT_TOKEN|TG_BOT_TOKEN|WATCH_URL_TOKEN|API_KEY)\b\s*=?\s*)[^\s<>\"]+",
    re.IGNORECASE,
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9._-]+\b")
OPENAI_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{6,}\b")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _now() -> datetime:
    return datetime.now(tz=SAST_TZ)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SAST_TZ)
    return parsed.astimezone(SAST_TZ)


def _fmt_dt(value: Optional[datetime]) -> str:
    return (value or _now()).astimezone(SAST_TZ).strftime("%H:%M SAST")


def _redact(text: str) -> str:
    text = text.replace("\r", "")
    text = ANSI_RE.sub("", text)
    text = WATCH_URL_RE.sub("[watch link redacted]", text)
    text = TOKEN_KV_RE.sub(r"\1[redacted]", text)
    text = SENSITIVE_KV_RE.sub(r"\1[redacted]", text)
    text = JWT_RE.sub("[jwt redacted]", text)
    text = OPENAI_TOKEN_RE.sub("[redacted]", text)
    return text


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


def _append_unique(values: List[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _score_delta_text(delta: Optional[int]) -> str:
    if delta is None:
        return "n/a"
    return f"{delta:+d}"


def _agent_label(agent_id: str) -> str:
    if agent_id in HOUSE_BOT_IDS:
        return f"{agent_id} (house bot)"
    return agent_id


@dataclass
class RoundMetrics:
    round_id: str
    requests_sent: List[str] = field(default_factory=list)
    requests_received: List[str] = field(default_factory=list)
    signed_replies_received: List[str] = field(default_factory=list)
    signatures_submitted: List[str] = field(default_factory=list)
    signed_requests_sent: List[str] = field(default_factory=list)
    action_reminders: int = 0
    declines: Dict[str, int] = field(default_factory=dict)
    unauthorized_skips: int = 0
    stale_skips: int = 0
    missing_signer_skips: int = 0
    parser_fallbacks: int = 0


@dataclass
class MatchMetrics:
    index: int
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    ended: bool = False
    rounds: Dict[str, RoundMetrics] = field(default_factory=dict)
    disconnects: int = 0
    reconnects: int = 0

    def total_reminders(self) -> int:
        return sum(round_metrics.action_reminders for round_metrics in self.rounds.values())

    def total_submissions(self) -> int:
        return sum(len(round_metrics.signatures_submitted) for round_metrics in self.rounds.values())

    def total_signed_replies(self) -> int:
        return sum(len(round_metrics.signed_replies_received) for round_metrics in self.rounds.values())

    def total_requests_sent(self) -> int:
        return sum(len(round_metrics.requests_sent) for round_metrics in self.rounds.values())


@dataclass
class CoachAnalysis:
    rank: Optional[int]
    score: Optional[int]
    gap_to_four: Optional[int]
    gap_to_one: Optional[int]
    deltas: Dict[int, Optional[int]]
    rank_delta: Optional[int]
    matches: List[MatchMetrics]
    weaknesses: List[str]
    recommendation_title: str
    recommendation_reason: str
    recommendation_goal: str
    recommendation_evidence: List[str]
    latest_log_stale: bool
    log_age_seconds: Optional[float]
    match_review_exists: bool
    match_review_notes: List[str]
    state: Dict[str, Any]


class EmailGameCoach:
    def __init__(
        self,
        log_file: Path = LOG_FILE,
        monitor_state_file: Path = MONITOR_STATE_FILE,
        leaderboard_state_file: Path = LEADERBOARD_STATE_FILE,
        coach_state_file: Path = COACH_STATE_FILE,
        match_review_file: Path = MATCH_REVIEW_FILE,
    ) -> None:
        load_dotenv(PROJECT_ROOT / ".env.local")
        self.log_file = log_file
        self.monitor_state_file = monitor_state_file
        self.leaderboard_state_file = leaderboard_state_file
        self.coach_state_file = coach_state_file
        self.match_review_file = match_review_file
        self.agent_name = os.getenv("EMAIL_GAME_AGENT_NAME", "").strip() or AGENT_NAME_DEFAULT
        self.state = _read_json(coach_state_file)

    def analyze(self, persist: bool = True) -> CoachAnalysis:
        leaderboard_snapshot = self._leaderboard_snapshot()
        if persist:
            self._record_leaderboard_snapshot(leaderboard_snapshot)
        history = self._leaderboard_history()
        matches = self._parse_matches()
        latest_matches = matches[-MAX_MATCHES:]
        deltas = {minutes: self._score_delta(history, minutes) for minutes in (15, 30, 60)}
        rank_delta = self._rank_delta(history)
        log_age_seconds = self._log_age_seconds()
        latest_log_stale = bool(log_age_seconds is not None and log_age_seconds > 300)
        match_review_exists, match_review_notes = self._match_review_notes()
        weaknesses = self._detect_weaknesses(latest_matches, latest_log_stale)
        recommendation = self._recommend(latest_matches, weaknesses, deltas, rank_delta, latest_log_stale, match_review_notes)

        if persist:
            self.state["last_analysis_at"] = _now().isoformat()
            _write_json(self.coach_state_file, self.state)

        return CoachAnalysis(
            rank=_int_or_none(leaderboard_snapshot.get("rank")),
            score=_int_or_none(leaderboard_snapshot.get("score")),
            gap_to_four=_int_or_none(leaderboard_snapshot.get("gap_to_four")),
            gap_to_one=_int_or_none(leaderboard_snapshot.get("gap_to_one")),
            deltas=deltas,
            rank_delta=rank_delta,
            matches=latest_matches,
            weaknesses=weaknesses,
            recommendation_title=recommendation[0],
            recommendation_reason=recommendation[1],
            recommendation_goal=recommendation[2],
            recommendation_evidence=recommendation[3],
            latest_log_stale=latest_log_stale,
            log_age_seconds=log_age_seconds,
            match_review_exists=match_review_exists,
            match_review_notes=match_review_notes,
            state=self.state,
        )

    def telegram_coach_text(self) -> str:
        analysis = self.analyze(persist=True)
        if analysis.latest_log_stale:
            lines = [
                "Email Game Coach",
                "",
                f"Rank: #{analysis.rank}" if analysis.rank is not None else "Rank: n/a",
                f"Score trend: {_score_delta_text(analysis.deltas.get(30))} in 30m",
                f"Gap to #4: {analysis.gap_to_four if analysis.gap_to_four is not None else 'n/a'}",
                f"Gap to #1: {analysis.gap_to_one if analysis.gap_to_one is not None else 'n/a'}",
                "",
                "Monitoring stale: yes",
                "Diagnosis: local match summaries are withheld until the live log stream is fresh.",
                "",
                "Next recommendation:",
                analysis.recommendation_title,
            ]
            return self._html(lines)
        latest = analysis.matches[-3:]
        lines = [
            "Email Game Coach",
            "",
            f"Rank: #{analysis.rank}" if analysis.rank is not None else "Rank: n/a",
            f"Score trend: {_score_delta_text(analysis.deltas.get(30))} in 30m",
            f"Gap to #4: {analysis.gap_to_four if analysis.gap_to_four is not None else 'n/a'}",
            f"Gap to #1: {analysis.gap_to_one if analysis.gap_to_one is not None else 'n/a'}",
            "",
            "Last 3 matches:",
        ]
        if latest:
            for offset, match in enumerate(latest, 1):
                lines.append(
                    f"- Match {offset}: reminders {match.total_reminders()}, "
                    f"submissions {match.total_submissions()}, signed replies {match.total_signed_replies()}"
                )
        else:
            lines.append("- none parsed yet")
        lines.extend(["", "Diagnosis:"])
        if analysis.weaknesses:
            lines.extend(f"- {item}" for item in analysis.weaknesses[:6])
        else:
            lines.append("- No current weakness detected from local evidence.")
        lines.extend(["", "Next recommendation:", analysis.recommendation_title])
        return self._html(lines)

    def telegram_recommend_text(self) -> str:
        analysis = self.analyze(persist=True)
        lines = [
            "Recommended Codex Goal",
            "",
            f"Priority: {analysis.recommendation_title.split(':', 1)[0]}",
            f"Reason: {analysis.recommendation_reason}",
            "",
            "Suggested goal:",
            analysis.recommendation_goal,
            "",
            "Evidence:",
        ]
        lines.extend(f"- {item}" for item in analysis.recommendation_evidence)
        lines.extend(["", "Action:", "Review the recommendation before approving implementation."])
        return self._html(lines)

    def telegram_reviewmatch_text(self) -> str:
        analysis = self.analyze(persist=True)
        if analysis.latest_log_stale:
            return self._html(
                [
                    "Latest Match Diagnosis",
                    "",
                    "Monitoring stale: yes",
                    "Match diagnosis is withheld until the live log stream is fresh.",
                    "",
                    "Next recommendation:",
                    analysis.recommendation_title,
                ]
            )
        match = analysis.matches[-1] if analysis.matches else None
        if match is None:
            return "No match diagnosis available yet."
        lines = [
            "Latest Match Diagnosis",
            "",
            f"Started: {_fmt_dt(match.started_at)}",
            f"Status: {'ended' if match.ended else 'in progress'}",
            f"Rounds: {len(match.rounds)}",
            f"Requests sent: {match.total_requests_sent()}",
            f"Signed replies received: {match.total_signed_replies()}",
            f"Signatures submitted: {match.total_submissions()}",
            f"Action reminders: {match.total_reminders()}",
            "",
            "Rounds:",
        ]
        for round_id, round_metrics in self._sorted_rounds(match):
            lines.append(
                f"- Round {round_id}: sent {len(round_metrics.requests_sent)}, "
                f"replies {len(round_metrics.signed_replies_received)}, "
                f"submitted {len(round_metrics.signatures_submitted)}, "
                f"reminders {round_metrics.action_reminders}"
            )
        return self._html(lines)

    def telegram_metrics_text(self) -> str:
        analysis = self.analyze(persist=True)
        recent = analysis.matches[-3:]
        lines = [
            "Email Game Metrics",
            "",
            f"Score: {analysis.score if analysis.score is not None else 'n/a'}",
            f"Rank: #{analysis.rank}" if analysis.rank is not None else "Rank: n/a",
            f"Score delta 15m: {_score_delta_text(analysis.deltas.get(15))}",
            f"Score delta 30m: {_score_delta_text(analysis.deltas.get(30))}",
            f"Score delta 60m: {_score_delta_text(analysis.deltas.get(60))}",
            f"Gap to #4: {analysis.gap_to_four if analysis.gap_to_four is not None else 'n/a'}",
            f"Gap to #1: {analysis.gap_to_one if analysis.gap_to_one is not None else 'n/a'}",
            f"Matches parsed: {len(analysis.matches)}",
            f"Recent reminders: {sum(match.total_reminders() for match in recent)}",
            f"Recent submissions: {sum(match.total_submissions() for match in recent)}",
            f"Recent signed replies: {sum(match.total_signed_replies() for match in recent)}",
            f"Log stale: {'yes' if analysis.latest_log_stale else 'no'}",
            f"Local review notes: {'yes' if analysis.match_review_exists else 'no'}",
        ]
        return self._html(lines)

    def maybe_alert_text(self, reason: str = "") -> Optional[str]:
        analysis = self.analyze(persist=True)
        alert_key, alert_reason = self._alert_condition(analysis, reason)
        if not alert_key:
            return None

        now_ts = time.time()
        last_alert = self.state.get("last_alert")
        if isinstance(last_alert, dict):
            last_key = str(last_alert.get("key") or "")
            last_at = float(last_alert.get("at") or 0)
            if last_key == alert_key and now_ts - last_at < ALERT_COOLDOWN_SECONDS:
                _write_json(self.coach_state_file, self.state)
                return None
            if now_ts - last_at < ALERT_COOLDOWN_SECONDS and alert_key not in {"disconnect", "log_stale"}:
                _write_json(self.coach_state_file, self.state)
                return None

        self.state["last_alert"] = {"key": alert_key, "at": now_ts, "reason": alert_reason}
        _write_json(self.coach_state_file, self.state)
        return self._html(
            [
                "Email Game Coach Recommendation",
                "",
                f"Trigger: {alert_reason}",
                f"Rank: #{analysis.rank}" if analysis.rank is not None else "Rank: n/a",
                f"Score: {analysis.score if analysis.score is not None else 'n/a'}",
                f"30m trend: {_score_delta_text(analysis.deltas.get(30))}",
                "",
                "Recommendation:",
                analysis.recommendation_title,
                analysis.recommendation_goal,
            ]
        )

    def append_match_review_note(self) -> bool:
        analysis = self.analyze(persist=True)
        latest = analysis.matches[-1] if analysis.matches else None
        timestamp = _now().isoformat()
        lines = [
            f"## Email Game Coach - {timestamp}",
            "",
            f"- Rank: #{analysis.rank}" if analysis.rank is not None else "- Rank: n/a",
            f"- Score: {analysis.score if analysis.score is not None else 'n/a'}",
            f"- Recommendation: {analysis.recommendation_title}",
        ]
        if latest:
            lines.append(
                f"- Latest match: rounds {len(latest.rounds)}, reminders {latest.total_reminders()}, "
                f"submissions {latest.total_submissions()}"
            )
        lines.append("")
        self.match_review_file.write_text(
            (self.match_review_file.read_text(encoding="utf-8") if self.match_review_file.exists() else "")
            + "\n".join(lines)
            + "\n",
            encoding="utf-8",
        )
        return True

    def _leaderboard_snapshot(self) -> Dict[str, Any]:
        data = _read_json(self.leaderboard_state_file)
        snapshot = data.get("last_snapshot") if isinstance(data, dict) else None
        return snapshot if isinstance(snapshot, dict) else {}

    def _record_leaderboard_snapshot(self, snapshot: Dict[str, Any]) -> None:
        if not snapshot:
            return
        fetched_at = str(snapshot.get("fetched_at") or _now().isoformat())
        entry = {
            "fetched_at": fetched_at,
            "rank": _int_or_none(snapshot.get("rank")),
            "score": _int_or_none(snapshot.get("score")),
            "gap_to_four": _int_or_none(snapshot.get("gap_to_four")),
            "gap_to_one": _int_or_none(snapshot.get("gap_to_one")),
        }
        history = self._leaderboard_history()
        if not history or str(history[-1].get("fetched_at")) != fetched_at:
            history.append(entry)
        self.state["leaderboard_history"] = history[-MAX_HISTORY:]

    def _leaderboard_history(self) -> List[Dict[str, Any]]:
        history = self.state.get("leaderboard_history")
        if not isinstance(history, list):
            return []
        return [item for item in history if isinstance(item, dict)]

    def _score_delta(self, history: List[Dict[str, Any]], minutes: int) -> Optional[int]:
        current = self._latest_history_with_score(history)
        if current is None:
            return None
        current_time = _parse_dt(current.get("fetched_at"))
        current_score = _int_or_none(current.get("score"))
        if current_time is None or current_score is None:
            return None
        cutoff = current_time - timedelta(minutes=minutes)
        previous = None
        for item in history:
            item_time = _parse_dt(item.get("fetched_at"))
            if item_time is not None and item_time <= cutoff and _int_or_none(item.get("score")) is not None:
                previous = item
        if previous is None and len(history) >= 2:
            previous = history[0]
        previous_score = _int_or_none(previous.get("score")) if previous else None
        if previous_score is None:
            return None
        return current_score - previous_score

    def _rank_delta(self, history: List[Dict[str, Any]]) -> Optional[int]:
        current = None
        previous = None
        for item in history:
            if _int_or_none(item.get("rank")) is not None:
                previous = current
                current = item
        if current is None or previous is None:
            return None
        current_rank = _int_or_none(current.get("rank"))
        previous_rank = _int_or_none(previous.get("rank"))
        if current_rank is None or previous_rank is None:
            return None
        return current_rank - previous_rank

    def _latest_history_with_score(self, history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for item in reversed(history):
            if _int_or_none(item.get("score")) is not None:
                return item
        return None

    def _log_age_seconds(self) -> Optional[float]:
        if not self.log_file.exists():
            return None
        try:
            return max(0.0, time.time() - self.log_file.stat().st_mtime)
        except OSError:
            return None

    def _read_log_lines(self) -> List[str]:
        if not self.log_file.exists():
            return []
        try:
            with self.log_file.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - MAX_LOG_BYTES))
                data = handle.read().decode("utf-8", "replace")
        except OSError:
            return []
        return [_redact(line.strip()) for line in data.splitlines() if line.strip()]

    def _match_review_notes(self) -> Tuple[bool, List[str]]:
        if not self.match_review_file.exists():
            return False, []
        try:
            lines = [_redact(line.strip()) for line in self.match_review_file.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return True, ["MATCH_REVIEW.md exists but could not be read."]
        notes: List[str] = []
        capture = False
        for line in lines:
            lower = line.lower()
            if lower.startswith("## risks") or lower.startswith("## open") or lower.startswith("## current status"):
                capture = True
                continue
            if lower.startswith("## ") and capture:
                capture = False
            if capture and line.startswith("- "):
                notes.append(line[2:])
        return True, notes[:5]

    def _parse_matches(self) -> List[MatchMetrics]:
        matches: List[MatchMetrics] = []
        current: Optional[MatchMetrics] = None
        current_round_id: Optional[str] = None

        for raw_line in self._read_log_lines():
            line = raw_line.strip()
            if not line:
                continue
            if self._is_match_start(line):
                current = MatchMetrics(index=len(matches) + 1, started_at=_now())
                matches.append(current)
                current_round_id = None
                continue
            if current is None:
                current = MatchMetrics(index=len(matches) + 1)
                matches.append(current)

            round_id = self._extract_round_id(line)
            if round_id:
                current_round_id = round_id
                self._round(current, round_id)

            if self._is_match_end(line):
                current.ended = True
                current.ended_at = _now()
                current = None
                current_round_id = None
                continue

            if "disconnected" in line.lower() or "connection dropped" in line.lower() or "connection error" in line.lower():
                current.disconnects += 1
            if "reconnected" in line.lower() or "reconnect" in line.lower():
                current.reconnects += 1

            if "[INFO] Round " in line and "fuzzy=" in line:
                round_metrics = self._round(current, current_round_id or round_id or "unknown")
                fuzzy_match = re.search(r"fuzzy=(\d+)", line)
                round_metrics.parser_fallbacks += int(fuzzy_match.group(1)) if fuzzy_match else 0

            if "Sent signature request to " in line:
                agent = line.rsplit("Sent signature request to ", 1)[-1].strip()
                self._append_to_round(current, current_round_id, "requests_sent", agent)
                continue

            signed_request = re.search(r"Signed request from ([^ ]+)", line, re.IGNORECASE)
            if signed_request:
                self._append_to_round(current, current_round_id, "signed_requests_sent", signed_request.group(1).strip())
                continue

            received_signed_payload = re.search(r"Received signed payload: signer=([^ ]+)", line, re.IGNORECASE)
            if received_signed_payload:
                self._append_to_round(current, current_round_id, "signed_replies_received", received_signed_payload.group(1).strip())
                continue

            submitted = re.search(r"submitted signature for round (\d+) from ([^ ]+)", line, re.IGNORECASE)
            if submitted:
                round_metrics = self._round(current, submitted.group(1))
                _append_unique(round_metrics.signatures_submitted, submitted.group(2).strip())
                continue

            submitted_legacy = re.search(r"Submitted received signature from ([^ ]+)", line, re.IGNORECASE)
            if submitted_legacy:
                self._append_to_round(current, current_round_id, "signatures_submitted", submitted_legacy.group(1).strip())
                continue

            submitted_agent_log = re.search(
                r"submitted signature \(by ([^ ]+) for [^)]+\)",
                line,
                re.IGNORECASE,
            )
            if submitted_agent_log:
                self._append_to_round(current, current_round_id, "signatures_submitted", submitted_agent_log.group(1).strip())
                continue

            inbound = re.search(r"received from ([^:]+):\s*(.*?)\s*(?:\(Round\s+(\d+)\))?$", line, re.IGNORECASE)
            if inbound:
                agent = inbound.group(1).strip()
                subject = inbound.group(2).strip()
                inbound_round = inbound.group(3) or current_round_id or "unknown"
                round_metrics = self._round(current, inbound_round)
                subject_lower = subject.lower()
                if "action completion reminder" in subject_lower:
                    round_metrics.action_reminders += 1
                elif "declin" in subject_lower:
                    round_metrics.declines[agent] = round_metrics.declines.get(agent, 0) + 1
                elif self._looks_like_signed_reply(subject_lower):
                    _append_unique(round_metrics.signed_replies_received, agent)
                elif self._looks_like_signature_request(subject_lower):
                    _append_unique(round_metrics.requests_received, agent)
                continue

            if "Skipped because unauthorized" in line:
                self._increment_round(current, current_round_id, "unauthorized_skips")
            elif "Skipped because stale" in line:
                self._increment_round(current, current_round_id, "stale_skips")
            elif "Skipped because missing required signer" in line:
                self._increment_round(current, current_round_id, "missing_signer_skips")

        return [match for match in matches if match.rounds or match.ended][-MAX_MATCHES:]

    def _is_match_start(self, line: str) -> bool:
        lower = line.lower()
        return "match found - game starting!" in lower or "in game - round 1" in lower

    def _is_match_end(self, line: str) -> bool:
        return "game over - between matches now" in line.lower()

    def _extract_round_id(self, line: str) -> Optional[str]:
        patterns = (
            r"IN GAME - Round\s+(\d+)",
            r"\[INFO\]\s*Round\s+(\d+):",
            r"Round\s+(\d+)\b.*Instructions",
            r"\(Round\s+(\d+)\)",
        )
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _round(self, match: MatchMetrics, round_id: str) -> RoundMetrics:
        round_metrics = match.rounds.get(round_id)
        if round_metrics is None:
            round_metrics = RoundMetrics(round_id=round_id)
            match.rounds[round_id] = round_metrics
        return round_metrics

    def _append_to_round(self, match: MatchMetrics, round_id: Optional[str], attr: str, value: str) -> None:
        round_metrics = self._round(match, round_id or self._latest_round_id(match) or "unknown")
        values = getattr(round_metrics, attr)
        _append_unique(values, value)

    def _increment_round(self, match: MatchMetrics, round_id: Optional[str], attr: str) -> None:
        round_metrics = self._round(match, round_id or self._latest_round_id(match) or "unknown")
        setattr(round_metrics, attr, int(getattr(round_metrics, attr)) + 1)

    def _latest_round_id(self, match: MatchMetrics) -> Optional[str]:
        if not match.rounds:
            return None
        return sorted(match.rounds.keys(), key=lambda value: (0, int(value)) if value.isdigit() else (1, value))[-1]

    def _looks_like_signature_request(self, subject_lower: str) -> bool:
        if "signature request response" in subject_lower:
            return False
        if "declin" in subject_lower:
            return False
        return (
            "signature request" in subject_lower
            or "request for signature" in subject_lower
            or "requesting your signature" in subject_lower
            or "please sign" in subject_lower
            or subject_lower.startswith("request for sign")
        )

    def _looks_like_signed_reply(self, subject_lower: str) -> bool:
        return False

    def _sorted_rounds(self, match: MatchMetrics) -> List[Tuple[str, RoundMetrics]]:
        return sorted(match.rounds.items(), key=lambda item: (0, int(item[0])) if item[0].isdigit() else (1, item[0]))

    def _detect_weaknesses(self, matches: List[MatchMetrics], latest_log_stale: bool) -> List[str]:
        weaknesses: List[str] = []
        recent = matches[-3:]
        if latest_log_stale:
            weaknesses.append("Monitor log stream is stale.")
        if not recent:
            weaknesses.append("No recent matches parsed from local logs.")
            return weaknesses

        r23_rounds = [
            round_metrics
            for match in recent
            for round_id, round_metrics in match.rounds.items()
            if round_id in {"2", "3"}
        ]
        if any(len(round_metrics.requests_sent) == 0 for round_metrics in r23_rounds):
            weaknesses.append("Round 2/3 request fanout missing in at least one recent round.")
        elif r23_rounds:
            weaknesses.append("Round 2/3 fanout is present in recent rounds.")

        missed_submissions = sum(
            max(0, len(round_metrics.signed_replies_received) - len(round_metrics.signatures_submitted))
            for match in recent
            for round_metrics in match.rounds.values()
        )
        if missed_submissions:
            weaknesses.append(f"{missed_submissions} signed replies were seen without matching submission logs.")
        if any(match.total_reminders() > 0 for match in recent):
            weaknesses.append("Action reminders still appear in recent matches.")
        if len(recent) >= 2 and all(match.total_submissions() == 0 for match in recent[-2:]):
            weaknesses.append("No signature submissions were observed for 2 consecutive matches.")
        decline_counts: Dict[str, int] = {}
        for match in recent:
            for round_metrics in match.rounds.values():
                for agent, count in round_metrics.declines.items():
                    decline_counts[agent] = decline_counts.get(agent, 0) + count
        if decline_counts:
            agent, count = max(decline_counts.items(), key=lambda item: item[1])
            weaknesses.append(f"{_agent_label(agent)} declined most often ({count} recent decline events).")
        stale_skips = sum(round_metrics.stale_skips for match in recent for round_metrics in match.rounds.values())
        if stale_skips:
            weaknesses.append(f"{stale_skips} stale signed payload skip(s) observed recently.")
        missing_signer = sum(round_metrics.missing_signer_skips for match in recent for round_metrics in match.rounds.values())
        if missing_signer:
            weaknesses.append(f"{missing_signer} missing-required-signer skip(s) observed recently.")
        fallback = sum(round_metrics.parser_fallbacks for match in recent for round_metrics in match.rounds.values())
        if fallback:
            weaknesses.append(f"Parser fallback used {fallback} time(s) in recent rounds.")
        return weaknesses

    def _recommend(
        self,
        matches: List[MatchMetrics],
        weaknesses: List[str],
        deltas: Dict[int, Optional[int]],
        rank_delta: Optional[int],
        latest_log_stale: bool,
        match_review_notes: List[str],
    ) -> Tuple[str, str, str, List[str]]:
        recent = matches[-3:]
        evidence: List[str] = []
        if latest_log_stale:
            return (
                "High: restore monitor log freshness",
                "The coach cannot trust stale logs.",
                "email-game-restore-monitor-log-stream",
                ["Log stream is stale according to file mtime."],
            )

        signed_without_submitted = sum(
            max(0, len(round_metrics.signed_replies_received) - len(round_metrics.signatures_submitted))
            for match in recent
            for round_metrics in match.rounds.values()
        )
        if signed_without_submitted:
            evidence.append(f"{signed_without_submitted} signed reply/replies lacked matching submission logs.")
            evidence.extend(f"MATCH_REVIEW: {note}" for note in match_review_notes[:2])
            return (
                "High: improve signed-reply submission confirmation",
                "Signed replies appear without matching submitted-signature evidence.",
                "email-game-improve-signed-reply-submission-confirmation",
                evidence,
            )

        r23_missing = [
            round_id
            for match in recent
            for round_id, round_metrics in match.rounds.items()
            if round_id in {"2", "3"} and not round_metrics.requests_sent
        ]
        if r23_missing:
            evidence.append(f"Round 2/3 fanout missing in {len(r23_missing)} recent round(s).")
            return (
                "High: improve Round 2/3 request target parsing",
                "Round 2/3 instructions are not consistently producing request targets.",
                "email-game-improve-round-2-3-request-target-parsing",
                evidence,
            )

        consecutive_reminders = len(recent) >= 2 and all(match.total_reminders() > 0 for match in recent[-2:])
        if consecutive_reminders:
            evidence.append("Action reminders appeared in 2 consecutive recent matches.")
            return (
                "Medium: reduce action reminders after valid work",
                "Score pressure correlates with rounds that still trigger reminders.",
                "email-game-investigate-action-reminder-timing",
                evidence,
            )

        if rank_delta is not None and rank_delta > 0:
            evidence.append(f"Rank worsened by {rank_delta} place(s) across coach history.")
        if deltas.get(30) is not None and deltas[30] < -5:
            evidence.append(f"Score delta over 30m is {deltas[30]}.")
        if evidence:
            return (
                "Medium: review recent scoring regressions",
                "Leaderboard movement declined while local logs show active matches.",
                "email-game-review-recent-score-regression",
                evidence,
            )

        if recent:
            evidence.append(
                f"Last {len(recent)} match(es): {sum(match.total_submissions() for match in recent)} submissions, "
                f"{sum(match.total_reminders() for match in recent)} reminders."
            )
        else:
            evidence.append("No completed recent match evidence is available yet.")
        return (
            "Low: continue monitoring before agent edits",
            "No proven code issue requires implementation right now.",
            "email-game-continue-performance-monitoring",
            evidence,
        )

    def _alert_condition(self, analysis: CoachAnalysis, reason: str) -> Tuple[str, str]:
        history = self._leaderboard_history()
        if len(history) >= 2:
            current = history[-1]
            previous = history[-2]
            current_score = _int_or_none(current.get("score"))
            previous_score = _int_or_none(previous.get("score"))
            current_rank = _int_or_none(current.get("rank"))
            previous_rank = _int_or_none(previous.get("rank"))
            if current_score is not None and previous_score is not None and current_score - previous_score < -5:
                return "score_drop", f"Score dropped {current_score - previous_score} across leaderboard polls."
            if current_rank is not None and previous_rank is not None and current_rank > previous_rank:
                return "rank_drop", f"Rank dropped from #{previous_rank} to #{current_rank}."
            if current_score is not None and previous_score is not None and current_score - previous_score >= 20:
                return "score_improved", f"Score improved {current_score - previous_score} after recent changes."

        recent = analysis.matches[-2:]
        if len(recent) >= 2 and all(match.total_reminders() > 0 for match in recent):
            return "consecutive_reminders", "Action reminders appeared in 2 consecutive matches."
        if len(recent) >= 2 and all(match.total_submissions() == 0 for match in recent):
            return "no_submissions", "No signature submissions were observed for 2 consecutive matches."
        if reason == "disconnect" or any(match.disconnects for match in recent):
            return "disconnect", "Agent disconnect event observed."
        if analysis.latest_log_stale or reason == "log_stale":
            return "log_stale", "Monitor log stream is stale."
        return "", ""

    def _html(self, lines: Iterable[str]) -> str:
        rendered: List[str] = []
        for line in lines:
            if not line:
                rendered.append("")
            elif line.endswith(":") or line in {
                "Email Game Coach",
                "Recommended Codex Goal",
                "Latest Match Diagnosis",
                "Email Game Metrics",
                "Email Game Coach Recommendation",
            }:
                rendered.append(f"<b>{html_escape(line, quote=False)}</b>")
            else:
                rendered.append(html_escape(line, quote=False))
        return "\n".join(rendered).strip()


def main() -> int:
    coach = EmailGameCoach()
    print(coach.telegram_coach_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
