#!/usr/bin/env python3
"""Read-only local dashboard for The Email Game race control."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from dotenv import load_dotenv
import uvicorn

try:
    from emailgame_budget import EmailGameBudget
except ModuleNotFoundError:  # pragma: no cover - module-style imports in tests
    from scripts.emailgame_budget import EmailGameBudget

try:
    from emailgame_coach import EmailGameCoach
except ModuleNotFoundError:  # pragma: no cover - module-style imports in tests
    from scripts.emailgame_coach import EmailGameCoach


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_LOGS = PROJECT_ROOT / "agent_logs"
TOKEN_FILE = AGENT_LOGS / "emailgame-dashboard-token.txt"
URL_FILE = AGENT_LOGS / "emailgame-dashboard-url.txt"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

load_dotenv(PROJECT_ROOT / ".env.local")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_token_file() -> str:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = _read_text(TOKEN_FILE)
    if not token:
        token = secrets.token_urlsafe(24).rstrip("=")
        TOKEN_FILE.write_text(f"{token}\n", encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def _load_public_url() -> str:
    return _read_text(URL_FILE)


def _tmux_session_running(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except Exception:
        return None


def _sorted_rounds(match: Any) -> List[Dict[str, Any]]:
    rounds = getattr(match, "rounds", {}) or {}
    items: List[Dict[str, Any]] = []
    for round_id, metrics in rounds.items():
        requests_sent = len(getattr(metrics, "requests_sent", []) or [])
        request_targets = getattr(metrics, "request_targets", None)
        sent_count = max(requests_sent, request_targets or 0)
        items.append(
            {
                "round_id": str(round_id),
                "requests_sent": sent_count,
                "signed_replies_received": len(getattr(metrics, "signed_replies_received", []) or []),
                "signatures_submitted": len(getattr(metrics, "signatures_submitted", []) or []),
                "action_reminders": int(getattr(metrics, "action_reminders", 0) or 0),
                "parser_fallbacks": int(getattr(metrics, "parser_fallbacks", 0) or 0),
                "stale_skips": int(getattr(metrics, "stale_skips", 0) or 0),
                "unauthorized_skips": int(getattr(metrics, "unauthorized_skips", 0) or 0),
                "missing_signer_skips": int(getattr(metrics, "missing_signer_skips", 0) or 0),
            }
        )
    return sorted(items, key=lambda item: (0, int(item["round_id"])) if item["round_id"].isdigit() else (1, item["round_id"]))


def _sparkline_svg(points: List[int], width: int = 420, height: int = 120) -> str:
    if len(points) < 2:
        return (
            f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Leaderboard chart'>"
            "<rect width='100%' height='100%' rx='16' fill='rgba(255,255,255,0.04)'/>"
            "<text x='24' y='66' fill='rgba(255,255,255,0.6)' font-size='18'>Not enough history yet</text>"
            "</svg>"
        )
    min_value = min(points)
    max_value = max(points)
    span = max(max_value - min_value, 1)
    margin = 12
    usable_width = width - margin * 2
    usable_height = height - margin * 2
    step = usable_width / (len(points) - 1)
    coords: List[str] = []
    for index, value in enumerate(points):
        x = margin + index * step
        y = margin + (max_value - value) * usable_height / span
        coords.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(coords)
    latest = points[-1]
    first = points[0]
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Leaderboard score trend'>"
        f"<defs><linearGradient id='trend-fill' x1='0' x2='0' y1='0' y2='1'>"
        "<stop offset='0%' stop-color='rgba(90,198,255,0.45)'/>"
        "<stop offset='100%' stop-color='rgba(90,198,255,0.05)'/>"
        "</linearGradient></defs>"
        f"<rect width='100%' height='100%' rx='16' fill='rgba(255,255,255,0.04)'/>"
        f"<polyline fill='none' stroke='#5ac6ff' stroke-width='3' points='{polyline}' stroke-linecap='round' stroke-linejoin='round'/>"
        f"<text x='20' y='28' fill='rgba(255,255,255,0.75)' font-size='15'>score trend</text>"
        f"<text x='20' y='{height - 16}' fill='rgba(255,255,255,0.55)' font-size='12'>start {first}</text>"
        f"<text x='{width - 104}' y='{height - 16}' fill='rgba(255,255,255,0.55)' font-size='12'>latest {latest}</text>"
        "</svg>"
    )


def _dashboard_token_matches(candidate: str) -> bool:
    secret = _ensure_token_file()
    return bool(candidate and candidate == secret)


def _access_denied() -> PlainTextResponse:
    return PlainTextResponse("Dashboard link invalid or expired.", status_code=403)


def _request_token(request: Request, path_token: Optional[str] = None) -> str:
    return path_token or request.query_params.get("token", "")


def _summarize() -> Dict[str, Any]:
    coach = EmailGameCoach()
    coach_analysis = coach.analyze(persist=False)
    budget = EmailGameBudget()
    budget_analysis = budget.analyze(persist=False)

    leaderboard_state = _read_json(AGENT_LOGS / "emailgame-leaderboard-state.json")
    leaderboard_snapshot = leaderboard_state.get("last_snapshot")
    leaderboard_snapshot = leaderboard_snapshot if isinstance(leaderboard_snapshot, dict) else {}

    monitor_state = _read_json(AGENT_LOGS / "emailgame-monitor-state.json")
    phase = str(monitor_state.get("phase") or "waiting")
    last_event = str(monitor_state.get("last_event") or "")
    connected_at = str(monitor_state.get("connected_sent_at") or "")
    observed_lines = monitor_state.get("observed_lines") if isinstance(monitor_state.get("observed_lines"), list) else []
    latest_observed = observed_lines[-1] if observed_lines else {}
    latest_observed_text = str(latest_observed.get("text") or "") if isinstance(latest_observed, dict) else ""

    match = coach_analysis.matches[-1] if coach_analysis.matches else None
    latest_match = None
    if match is not None:
        latest_match = {
            "index": match.index,
            "started_at": match.started_at.isoformat() if match.started_at else None,
            "ended": bool(match.ended),
            "rounds": _sorted_rounds(match),
            "total_requests_sent": match.total_requests_sent(),
            "total_signed_replies": match.total_signed_replies(),
            "total_submissions": match.total_submissions(),
            "total_reminders": match.total_reminders(),
        }

    leaderboard_history = [
        {
            "fetched_at": str(item.get("fetched_at") or ""),
            "rank": _safe_int(item.get("rank")),
            "score": _safe_int(item.get("score")),
        }
        for item in coach_analysis.state.get("leaderboard_history", [])
        if isinstance(item, dict)
    ]
    score_points = [item["score"] for item in leaderboard_history if item["score"] is not None]

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": {
            "agent_running": _tmux_session_running("emailgame"),
            "monitor_running": _tmux_session_running("emailgame-monitor"),
            "dashboard_running": _tmux_session_running("emailgame-dashboard"),
            "phase": phase,
            "last_event": last_event,
            "connected_at": connected_at,
            "latest_observed": latest_observed_text,
            "log_stale": bool(coach_analysis.latest_log_stale),
        },
        "leaderboard": {
            "rank": coach_analysis.rank,
            "score": coach_analysis.score,
            "gap_to_four": coach_analysis.gap_to_four,
            "gap_to_one": coach_analysis.gap_to_one,
            "fetched_at": str(leaderboard_snapshot.get("fetched_at") or ""),
            "top5": leaderboard_snapshot.get("top5") if isinstance(leaderboard_snapshot.get("top5"), list) else [],
            "history": leaderboard_history[-12:],
            "chart": {
                "points": score_points[-12:],
            },
        },
        "coach": {
            "recommendation_title": coach_analysis.recommendation_title,
            "recommendation_reason": coach_analysis.recommendation_reason,
            "recommendation_goal": coach_analysis.recommendation_goal,
            "recommendation_evidence": coach_analysis.recommendation_evidence,
            "weaknesses": coach_analysis.weaknesses,
            "log_stale": bool(coach_analysis.latest_log_stale),
        },
        "metrics": {
            "score_delta_15m": coach_analysis.deltas.get(15),
            "score_delta_30m": coach_analysis.deltas.get(30),
            "score_delta_60m": coach_analysis.deltas.get(60),
            "recent_reminders": sum(match.total_reminders() for match in coach_analysis.matches[-3:]),
            "recent_submissions": sum(match.total_submissions() for match in coach_analysis.matches[-3:]),
            "recent_signed_replies": sum(match.total_signed_replies() for match in coach_analysis.matches[-3:]),
            "matches_parsed": len(coach_analysis.matches),
            "budget_usd": budget_analysis.budget_usd,
            "calls_15m": budget_analysis.calls_15m,
            "calls_30m": budget_analysis.calls_30m,
            "calls_60m": budget_analysis.calls_60m,
            "total_calls": budget_analysis.total_calls,
            "token_tracking_available": budget_analysis.token_tracking_available,
        },
        "latest_match": latest_match,
    }


def _html_page(summary: Dict[str, Any], public_url: str) -> str:
    status = summary["status"]
    leaderboard = summary["leaderboard"]
    coach = summary["coach"]
    metrics = summary["metrics"]
    latest_match = summary["latest_match"]
    top5 = leaderboard.get("top5") or []
    chart_points = leaderboard.get("chart", {}).get("points") or []
    chart_html = _sparkline_svg([int(value) for value in chart_points if isinstance(value, int) or isinstance(value, float)])
    last_status = status.get("phase", "waiting")
    button_html = (
        f"<a class='button' href='{html_escape(public_url, quote=True)}' target='_blank' rel='noreferrer'>Open Race Control Dashboard</a>"
        if public_url
        else "<span class='button muted'>Dashboard tunnel not active</span>"
    )
    rows = []
    for item in top5:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{html_escape(str(item.get('rank') or ''), quote=False)}</td>"
            f"<td>{html_escape(str(item.get('agent_id') or ''), quote=False)}</td>"
            f"<td>{html_escape(str(item.get('elo') or ''), quote=False)}</td>"
            "</tr>"
        )
    if latest_match:
        round_rows = []
        for round_info in latest_match.get("rounds", []):
            round_rows.append(
                "<tr>"
                f"<td>{html_escape(str(round_info.get('round_id') or ''), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('requests_sent') or 0), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('signed_replies_received') or 0), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('signatures_submitted') or 0), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('action_reminders') or 0), quote=False)}</td>"
                "</tr>"
            )
        round_table = "".join(round_rows)
    else:
        round_table = ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Email Game Race Control</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #09111f;
      --panel: rgba(11, 18, 30, 0.82);
      --panel-2: rgba(255,255,255,0.04);
      --text: #edf4ff;
      --muted: rgba(237, 244, 255, 0.72);
      --line: rgba(125, 165, 220, 0.24);
      --accent: #66d9ff;
      --accent-2: #7dffb2;
      --warning: #ffd166;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(102, 217, 255, 0.18), transparent 28%),
        radial-gradient(circle at 85% 0%, rgba(125, 255, 178, 0.14), transparent 26%),
        linear-gradient(180deg, #08101d 0%, #0b1321 55%, #08101b 100%);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }}
    .hero {{
      display: grid;
      gap: 16px;
      grid-template-columns: 1.3fr 0.7fr;
      align-items: start;
      margin-bottom: 16px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.2);
      backdrop-filter: blur(12px);
    }}
    .hero-main {{
      padding: 24px;
    }}
    .kicker {{
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.15em;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(28px, 5vw, 52px);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }}
    .sub {{
      margin-top: 14px;
      color: var(--muted);
      line-height: 1.5;
      max-width: 65ch;
    }}
    .button-row {{
      margin-top: 18px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 13px 18px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #07121f;
      text-decoration: none;
      font-weight: 800;
      border: none;
      cursor: pointer;
      box-shadow: 0 12px 24px rgba(102, 217, 255, 0.18);
    }}
    .button.muted {{
      background: rgba(255,255,255,0.08);
      color: var(--text);
      box-shadow: none;
      cursor: default;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      padding: 16px;
    }}
    .stat {{
      padding: 16px;
      background: var(--panel-2);
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,0.06);
      min-height: 110px;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .value {{
      font-size: clamp(24px, 3.5vw, 36px);
      font-weight: 800;
      letter-spacing: -0.04em;
    }}
    .small {{
      color: var(--muted);
      margin-top: 6px;
      font-size: 13px;
      line-height: 1.4;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-top: 16px;
    }}
    .panel {{
      padding: 18px;
    }}
    .panel h2 {{
      margin: 0 0 12px;
      font-size: 20px;
      letter-spacing: -0.02em;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      padding: 10px 8px;
      text-align: left;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      font-size: 14px;
    }}
    th {{
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 11px;
    }}
    .two-col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }}
    .chip {{
      display: inline-flex;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.07);
      color: var(--text);
      border: 1px solid rgba(255,255,255,0.08);
      font-size: 12px;
      margin: 0 6px 6px 0;
    }}
    .good {{ color: var(--accent-2); }}
    .warn {{ color: var(--warning); }}
    .chart svg {{ width: 100%; height: auto; display: block; }}
    ul {{
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
    }}
    .footer {{
      margin-top: 16px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 920px) {{
      .hero, .grid, .two-col, .stat-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="card hero-main">
        <div class="kicker">Email Game Race Control</div>
        <h1>🏁 Open Race Control Dashboard</h1>
        <div class="sub">
          Read-only view for the live Email Game agent, monitor, leaderboard, and coach signals.
          Protected access is required.
        </div>
        <div class="button-row">
          {button_html}
          <span class="chip">Agent { 'running' if status['agent_running'] else 'stopped' }</span>
          <span class="chip">Monitor { 'running' if status['monitor_running'] else 'stopped' }</span>
          <span class="chip">Phase: {html_escape(last_status, quote=False)}</span>
        </div>
      </div>
      <div class="card panel">
        <h2>Quick Status</h2>
        <div class="small"><span class="good">Rank</span> #{leaderboard.get('rank') or 'n/a'}</div>
        <div class="small"><span class="good">Score</span> {leaderboard.get('score') if leaderboard.get('score') is not None else 'n/a'}</div>
        <div class="small"><span class="warn">Gap to #4</span> {leaderboard.get('gap_to_four') if leaderboard.get('gap_to_four') is not None else 'n/a'}</div>
        <div class="small"><span class="warn">Gap to #1</span> {leaderboard.get('gap_to_one') if leaderboard.get('gap_to_one') is not None else 'n/a'}</div>
        <div class="small" style="margin-top: 12px;">Updated: {html_escape(str(summary.get('generated_at') or ''), quote=False)}</div>
      </div>
    </div>

    <div class="stat-grid card">
      <div class="stat">
        <div class="label">Coach</div>
        <div class="value">{html_escape(str(coach.get('recommendation_title') or 'n/a'), quote=False)}</div>
        <div class="small">{html_escape(str(coach.get('recommendation_reason') or ''), quote=False)}</div>
      </div>
      <div class="stat">
        <div class="label">Recent Match</div>
        <div class="value">{html_escape(str(latest_match.get('total_submissions') if latest_match else 0), quote=False)}</div>
        <div class="small">submissions, with {html_escape(str(latest_match.get('total_reminders') if latest_match else 0), quote=False)} reminders</div>
      </div>
      <div class="stat">
        <div class="label">Recent Signals</div>
        <div class="value">{html_escape(str(metrics.get('recent_signed_replies') or 0), quote=False)}</div>
        <div class="small">signed replies in the last 3 parsed matches</div>
      </div>
      <div class="stat">
        <div class="label">LLM Calls</div>
        <div class="value">{html_escape(str(metrics.get('total_calls') or 0), quote=False)}</div>
        <div class="small">{html_escape(str(metrics.get('calls_30m') or 0), quote=False)} in 30m, {html_escape(str(metrics.get('calls_60m') or 0), quote=False)} in 60m</div>
      </div>
    </div>

    <div class="grid">
      <div class="card panel chart">
        <h2>Leaderboard Chart</h2>
        {chart_html}
      </div>
      <div class="card panel">
        <h2>Leaderboard Top 5</h2>
        <table>
          <thead>
            <tr><th>Rank</th><th>Agent</th><th>Elo</th></tr>
          </thead>
          <tbody>
            {''.join(rows) if rows else '<tr><td colspan="3">No leaderboard snapshot yet.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid">
      <div class="card panel">
        <h2>Latest Match Summary</h2>
        {f"<div class='small'>Match #{latest_match.get('index')}</div>" if latest_match else "<div class='small'>No parsed match yet.</div>"}
        <div class="two-col" style="margin-top: 12px;">
          <div>
            <div class="label">Rounds</div>
            <table>
              <thead>
                <tr><th>Round</th><th>Sent</th><th>Replies</th><th>Submitted</th><th>Reminders</th></tr>
              </thead>
              <tbody>
                {round_table if latest_match else '<tr><td colspan="5">No parsed rounds yet.</td></tr>'}
              </tbody>
            </table>
          </div>
          <div>
            <div class="label">Coach Weaknesses</div>
            <ul>
              {''.join(f"<li>{html_escape(str(item), quote=False)}</li>" for item in (coach.get('weaknesses') or [])[:6]) or '<li>No weakness detected from local evidence.</li>'}
            </ul>
            <div class="label" style="margin-top: 14px;">Status Notes</div>
            <div class="small">{html_escape(str(status.get('last_event') or 'No recent event.'), quote=False)}</div>
            <div class="small">{html_escape(str(status.get('latest_observed') or ''), quote=False)}</div>
          </div>
        </div>
      </div>
      <div class="card panel">
        <h2>Metrics</h2>
        <div class="small">Score delta 15m: {metrics.get('score_delta_15m') if metrics.get('score_delta_15m') is not None else 'n/a'}</div>
        <div class="small">Score delta 30m: {metrics.get('score_delta_30m') if metrics.get('score_delta_30m') is not None else 'n/a'}</div>
        <div class="small">Score delta 60m: {metrics.get('score_delta_60m') if metrics.get('score_delta_60m') is not None else 'n/a'}</div>
        <div class="small">Recent reminders: {metrics.get('recent_reminders')}</div>
        <div class="small">Recent submissions: {metrics.get('recent_submissions')}</div>
        <div class="small">Recent signed replies: {metrics.get('recent_signed_replies')}</div>
        <div class="small">Log stale: {'yes' if status.get('log_stale') else 'no'}</div>
        <div class="small">Budget: ${metrics.get('budget_usd')}</div>
        <div class="small">Token tracking available: {'yes' if metrics.get('token_tracking_available') else 'no'}</div>
      </div>
    </div>

    <div class="footer">
      Protected via /d/&lt;token&gt;/ or ?token=&lt;token&gt;. No secrets or raw log dumps are exposed.
    </div>
  </div>
  <script>
    (function() {{
      const parts = window.location.pathname.split('/').filter(Boolean);
      const token = parts[0] === 'd' && parts[1] ? parts[1] : new URLSearchParams(window.location.search).get('token');
      if (!token) return;
      async function refresh() {{
        try {{
          const response = await fetch(`/d/${{encodeURIComponent(token)}}/api/dashboard`, {{
            cache: 'no-store',
            headers: {{ 'accept': 'application/json' }},
          }});
          if (!response.ok) return;
        }} catch (error) {{
        }}
      }}
      setInterval(refresh, 30000);
    }})();
  </script>
</body>
</html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="Email Game Race Control", docs_url=None, redoc_url=None)
    _ensure_token_file()

    def _check(request: Request, path_token: Optional[str] = None) -> Optional[PlainTextResponse]:
        if not _dashboard_token_matches(_request_token(request, path_token)):
            return _access_denied()
        return None

    def _json_response(request: Request, path_token: Optional[str] = None) -> Any:
        denied = _check(request, path_token)
        if denied is not None:
            return denied
        return JSONResponse(_summarize())

    def _html_response(request: Request, path_token: Optional[str] = None) -> Any:
        denied = _check(request, path_token)
        if denied is not None:
            return denied
        token = _request_token(request, path_token)
        return HTMLResponse(_html_page(_summarize(), _load_public_url()))

    @app.get("/")
    def root(request: Request):
        return _html_response(request)

    @app.get("/api/health")
    def api_health(request: Request):
        return _json_response(request)

    @app.get("/api/dashboard")
    def api_dashboard(request: Request):
        return _json_response(request)

    @app.get("/api/status")
    def api_status(request: Request):
        return JSONResponse({"status": _summarize()["status"]}) if _check(request) is None else _access_denied()

    @app.get("/api/leaderboard")
    def api_leaderboard(request: Request):
        return JSONResponse({"leaderboard": _summarize()["leaderboard"]}) if _check(request) is None else _access_denied()

    @app.get("/api/coach")
    def api_coach(request: Request):
        return JSONResponse({"coach": _summarize()["coach"]}) if _check(request) is None else _access_denied()

    @app.get("/api/metrics")
    def api_metrics(request: Request):
        return JSONResponse({"metrics": _summarize()["metrics"]}) if _check(request) is None else _access_denied()

    @app.get("/api/matches/latest")
    def api_latest_match(request: Request):
        return JSONResponse({"latest_match": _summarize()["latest_match"]}) if _check(request) is None else _access_denied()

    @app.get("/d/{token}/")
    def protected_root(token: str, request: Request):
        return _html_response(request, token)

    @app.get("/d/{token}")
    def protected_root_no_slash(token: str, request: Request):
        return _html_response(request, token)

    @app.get("/d/{token}/api/health")
    def protected_api_health(token: str, request: Request):
        return _json_response(request, token)

    @app.get("/d/{token}/api/dashboard")
    def protected_api_dashboard(token: str, request: Request):
        return _json_response(request, token)

    @app.get("/d/{token}/api/status")
    def protected_api_status(token: str, request: Request):
        return JSONResponse({"status": _summarize()["status"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/leaderboard")
    def protected_api_leaderboard(token: str, request: Request):
        return JSONResponse({"leaderboard": _summarize()["leaderboard"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/coach")
    def protected_api_coach(token: str, request: Request):
        return JSONResponse({"coach": _summarize()["coach"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/metrics")
    def protected_api_metrics(token: str, request: Request):
        return JSONResponse({"metrics": _summarize()["metrics"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/matches/latest")
    def protected_api_latest_match(token: str, request: Request):
        return JSONResponse({"latest_match": _summarize()["latest_match"]}) if _check(request, token) is None else _access_denied()

    return app


app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Email Game read-only dashboard.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
