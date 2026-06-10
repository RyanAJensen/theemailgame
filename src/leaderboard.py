"""Persistent cross-session Elo leaderboard for The Email Game.

The leaderboard is derived entirely from the session result JSON files in
session_results/ — there is no separate ratings database. Ratings are
recomputed by replaying every session in chronological order, so the
session files remain the single source of truth. A lightweight cache keyed
on the set of files + their modification times avoids recomputing on every
request while still picking up new sessions automatically.

Elo model
---------
Each game has up to NUM_AGENTS players. We treat a single game as a set of
pairwise matchups: for every ordered pair (i, j) the "actual" outcome is i's
share of the two agents' combined score, share = score_i / (score_i + score_j).
This is margin-aware — winning 8-0 (share 1.0) moves Elo more than winning 5-4
(share ~0.56) — while staying bounded in [0, 1] and zero-sum (share_ij +
share_ji = 1). The share is then passed through a concave blowout-dampening
curve (MARGIN_DAMPENING) so each extra point of dominance is worth less than
the last. Each agent's net rating change is the average of its pairwise
Elo deltas over its opponents, so one game moves a rating by at most ~K_FACTOR
regardless of player count. Deltas are computed from the pre-game ratings
(batch update) so the result is independent of pair order.
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.game.config import PROJECT_ROOT

INITIAL_RATING = 1000.0
K_FACTOR = 32.0

# Blowout dampening: a concave exponent applied to how far the score-share is
# from an even split. 1.0 = no dampening (reward grows linearly with margin);
# < 1.0 gives diminishing returns on margin so running up the score adds little
# beyond a solid win. 0.5 (square root) is the default. Stays bounded and
# zero-sum for any value in (0, 1].
MARGIN_DAMPENING = 0.5


def _margin_outcome(share: float) -> float:
    """Convert a raw score-share into a blowout-dampened 'actual' outcome.

    The deviation from 0.5 is raised to MARGIN_DAMPENING (concave), so the
    marginal value of each extra point of dominance decreases. Symmetric about
    0.5, so the two agents' outcomes still sum to 1 (zero-sum preserved).
    """
    d = share - 0.5
    if d == 0:
        return 0.5
    sign = 1.0 if d > 0 else -1.0
    return 0.5 + sign * 0.5 * (abs(2 * d) ** MARGIN_DAMPENING)

# Simple in-memory cache: (signature) -> computed entries
_cache_signature: Tuple = None
_cache_entries: List[Dict] = []


def competition_start() -> Optional[str]:
    """Return the competition start cutoff (ISO-8601) if configured, else None.

    When COMPETITION_START_TIME is set, only sessions that started at or after
    that timestamp count toward the leaderboard. This gives a clean board for a
    competition without deleting any session history.
    """
    val = os.environ.get("COMPETITION_START_TIME", "").strip()
    return val or None


def _results_dir() -> Path:
    return PROJECT_ROOT / "session_results"


def _session_files(results_dir: Path) -> List[Path]:
    return sorted(results_dir.glob("session_arena_*.json"))


def _signature(files: List[Path]) -> Tuple:
    """A signature that changes whenever sessions are added or modified."""
    return tuple((f.name, f.stat().st_mtime) for f in files)


def _load_sessions(results_dir: Path, cutoff: Optional[str] = None) -> List[Dict]:
    """Load valid session dicts sorted chronologically by start_time.

    If cutoff (ISO-8601) is given, sessions that started before it are skipped.
    """
    sessions = []
    for fp in _session_files(results_dir):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not d.get("cumulative_scores"):
            continue
        if cutoff and (d.get("start_time") or "") < cutoff:
            continue
        sessions.append(d)
    # start_time is ISO-8601 → lexicographic sort == chronological.
    # Fall back to session_id so ordering is stable if start_time missing.
    sessions.sort(key=lambda d: (d.get("start_time") or "", d.get("session_id") or ""))
    return sessions


def _expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _blank_stats() -> Dict:
    return {
        "games": 0, "wins": 0, "total_score": 0, "rounds": 0,
        "sub": 0, "sig": 0, "pen": 0,
        # attack/defense/collection accumulators (non-abandoned games only)
        "atk": 0, "atk_opp": 0, "def_fail": 0, "def_opp": 0,
        "authcoll": 0, "req": 0,
    }


def compute_leaderboard(results_dir: Path = None) -> List[Dict]:
    """Replay all sessions in order and return ranked leaderboard entries.

    Each entry: rank, agent_id, elo, games, wins, win_rate, total_score,
    avg_score_per_round, penalties.
    """
    global _cache_signature, _cache_entries

    results_dir = results_dir or _results_dir()
    if not results_dir.exists():
        return []

    cutoff = competition_start()
    files = _session_files(results_dir)
    sig = (cutoff,) + _signature(files)
    if sig == _cache_signature:
        return _cache_entries

    sessions = _load_sessions(results_dir, cutoff)

    ratings: Dict[str, float] = {}
    stats: Dict[str, Dict] = {}

    for d in sessions:
        scores: Dict[str, int] = d["cumulative_scores"]
        agents = list(scores.keys())
        if len(agents) < 2:
            continue

        # Agents who left mid-match. An abandoned game is a no-contest for the
        # survivors (their rating is frozen, the game isn't counted for them) and
        # a forfeit for the leavers (treated as a loss; only their rating moves).
        departed = set(d.get("departed", [])) & set(agents)
        abandoned = bool(departed)

        pre = {a: ratings.get(a, INITIAL_RATING) for a in agents}
        deltas = {a: 0.0 for a in agents}

        # Margin-aware outcome: a pairwise "actual" is the share of the two
        # agents' combined score rather than a binary win/loss, so winning by a
        # lot moves Elo more than winning by a little. Scores are shifted by the
        # game's minimum so penalty-driven negatives can't push a share outside
        # [0, 1]; this is a no-op when all scores are already non-negative. In an
        # abandoned game the leavers are scored as 0 (a loss) and only their
        # ratings update.
        update_set = departed if abandoned else set(agents)
        elo_scores = {a: (0 if (abandoned and a in departed) else scores[a]) for a in agents}
        floor = min(0, min(elo_scores.values()))
        shifted = {a: elo_scores[a] - floor for a in agents}

        for i in update_set:
            for j in agents:
                if i == j:
                    continue
                exp = _expected(pre[i], pre[j])
                total = shifted[i] + shifted[j]
                share = 0.5 if total == 0 else shifted[i] / total
                actual = _margin_outcome(share)
                deltas[i] += K_FACTOR * (actual - exp)

        num_opp = len(agents) - 1
        for a in update_set:
            ratings[a] = pre[a] + deltas[a] / num_opp

        # Aggregate stats. An abandoned game is not a real contest, so it counts
        # toward NO agent's games/win%/avg/round stats — neither the survivors
        # (no-contest) nor the leaver. The leaver's only consequence is the Elo
        # forfeit applied above (update_set = departed), which persists in their
        # rating for when they next complete a real game.
        num_rounds = d.get("total_rounds") or len(d.get("rounds", []))
        if abandoned:
            counted = set()
            sole_winner = None
        else:
            counted = set(agents)
            top = max(scores.values())
            winners = [a for a in agents if scores[a] == top]
            sole_winner = winners[0] if len(winners) == 1 else None

        for a in counted:
            st = stats.setdefault(a, _blank_stats())
            st["games"] += 1
            st["total_score"] += scores[a]
            st["rounds"] += num_rounds
            if a == sole_winner:
                st["wins"] += 1

        n = len(agents)
        for r in d.get("rounds", []):
            perf = r.get("agent_performance", {})
            req = r.get("request_lists", {})
            perm = r.get("signing_permissions", {})
            events = r.get("signature_events")
            for a, p in perf.items():
                if a not in counted or a not in stats:
                    continue
                stats[a]["sub"] += p.get("submission_points", 0)
                stats[a]["sig"] += p.get("signing_points", 0)
                stats[a]["pen"] += p.get("unauthorized_signing_penalties", 0)
                # Attack/defense/collection rates: skip abandoned (spoiled) games
                # so a walkout doesn't distort them. Denominators come from the
                # dual assignment structure; attacks/authorized-collections come
                # from the signature events (with a fallback for older files).
                if abandoned:
                    continue
                stats[a]["atk_opp"] += (n - 1) - len(req.get(a, []))
                stats[a]["def_opp"] += (n - 1) - len(perm.get(a, []))
                stats[a]["req"] += len(req.get(a, []))
                stats[a]["def_fail"] += p.get("unauthorized_signing_penalties", 0)
                if events is not None:
                    atk = sum(1 for e in events
                              if e.get("submitter") == a and not e.get("authorized"))
                    authcoll = sum(1 for e in events
                                   if e.get("submitter") == a and e.get("authorized"))
                else:
                    coll = p.get("successfully_submitted_for", [])
                    atk = sum(1 for y in coll if a not in perm.get(y, []))
                    authcoll = sum(1 for y in coll if a in perm.get(y, []))
                stats[a]["atk"] += atk
                stats[a]["authcoll"] += authcoll

    entries = []
    for a, st in stats.items():
        entries.append({
            "agent_id": a,
            "elo": round(ratings.get(a, INITIAL_RATING)),
            "games": st["games"],
            "wins": st["wins"],
            "win_rate": round(st["wins"] / st["games"], 3) if st["games"] else 0.0,
            "total_score": st["total_score"],
            "avg_score_per_round": round(st["total_score"] / st["rounds"], 2) if st["rounds"] else 0.0,
            "penalties": st["pen"],
            # Manipulation metrics (None when no opportunities seen yet)
            "attack_success_rate": (round(st["atk"] / st["atk_opp"], 3)
                                    if st["atk_opp"] else None),
            "defense_success_rate": (round((st["def_opp"] - st["def_fail"]) / st["def_opp"], 3)
                                     if st["def_opp"] else None),
            "collection_rate": (round(st["authcoll"] / st["req"], 3)
                                if st["req"] else None),
            "attacks_landed": st["atk"],
            "attack_opportunities": st["atk_opp"],
            "defense_failures": st["def_fail"],
            "defense_opportunities": st["def_opp"],
            "authorized_collected": st["authcoll"],
            "total_requests": st["req"],
        })

    entries.sort(key=lambda e: (-e["elo"], -e["games"]))
    for i, e in enumerate(entries, 1):
        e["rank"] = i

    _cache_signature = sig
    _cache_entries = entries
    return entries


# Inline SVG icons (Lucide-style, stroke = currentColor) used in place of emoji
# on the public leaderboard page. Self-contained so the page needs no asset host.
_ICON_TROPHY = (
    '<svg class="icon" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/>'
    '<path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/>'
    '<path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/>'
    '<path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/>'
    '<path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/></svg>'
)
_ICON_CALENDAR = (
    '<svg class="icon" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<rect x="3" y="4" width="18" height="18" rx="2"/>'
    '<path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/></svg>'
)
_ICON_MEDAL = (
    '<svg class="icon" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M7.21 15 2.66 7.14a2 2 0 0 1 .13-2.2L4.4 2.8A2 2 0 0 1 6 2h12a2 2 0 0 1 '
    '1.6.8l1.6 2.14a2 2 0 0 1 .14 2.2L16.79 15"/>'
    '<path d="M11 12 5.12 2.2"/><path d="m13 12 5.88-9.8"/><path d="M8 7h8"/>'
    '<circle cx="12" cy="17" r="5"/><path d="M12 18v-2h-.5"/></svg>'
)
# Metallic tints for the top three ranks (gold / silver / bronze).
_MEDAL_COLORS = {1: "#f5b301", 2: "#9aa3b0", 3: "#cd7f32"}


def _fmt_rate(x) -> str:
    return "&mdash;" if x is None else f"{x * 100:.0f}%"


def _rank_cell(rank: int) -> str:
    """Top three ranks get a tinted medal icon; the rest get a plain number.

    Both are wrapped in the same fixed-size centered badge so the numbers line
    up vertically and horizontally with the medals above them.
    """
    color = _MEDAL_COLORS.get(rank)
    if color:
        return (f'<span class="rank-badge medal" style="color:{color}" '
                f'aria-label="Rank {rank}">{_ICON_MEDAL}</span>')
    return f'<span class="rank-badge">{rank}</span>'


def render_leaderboard_html(entries: List[Dict], live: Dict = None) -> str:
    """Render the leaderboard as a standalone HTML page.

    ``live`` (optional) carries point-in-time arena activity for the header bar:
    ``{"players": int, "matches": int, "in_game": int, "queued": int}``.
    """
    if entries:
        rows = []
        for e in entries:
            rank_label = _rank_cell(e["rank"])
            rows.append(f"""
            <tr>
              <td class="rank">{rank_label}</td>
              <td class="agent"><a href="/agent/{_escape(e['agent_id'])}">{_escape(e['agent_id'])}</a></td>
              <td class="elo">{e['elo']}</td>
              <td>{e['games']}</td>
              <td>{e['wins']}</td>
              <td>{e['win_rate']*100:.0f}%</td>
              <td>{e['avg_score_per_round']:.2f}</td>
              <td>{_fmt_rate(e.get('attack_success_rate'))}&nbsp;/&nbsp;{_fmt_rate(e.get('defense_success_rate'))}</td>
              <td>{e['penalties']}</td>
            </tr>""")
        table_body = "".join(rows)
    else:
        table_body = """
            <tr><td colspan="9" class="empty">No games played yet. Run a session to populate the leaderboard.</td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>The Email Game Leaderboard</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      background: #f6f8fc; color: #202124; margin: 0; padding: 2rem 1rem;
    }}
    .wrap {{ max-width: 880px; margin: 0 auto; }}
    h1 {{ font-size: 1.7rem; margin: 0 0 .25rem; display: flex; align-items: center; gap: .55rem; }}
    .icon {{ display: inline-block; width: 1em; height: 1em; vertical-align: -0.125em;
      stroke: currentColor; fill: none; }}
    h1 .icon {{ color: #f5b301; }}
    .rank-badge {{ display: inline-flex; align-items: center; justify-content: center;
      width: 1.7em; height: 1.7em; line-height: 1; }}
    .rank-badge .icon {{ width: 1.3em; height: 1.3em; }}
    .sub {{ color: #5f6368; margin: 0 0 1.5rem; font-size: .95rem; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff;
      border: 1px solid #e3e6ea; border-radius: 12px; overflow: hidden;
      box-shadow: 0 1px 2px rgba(60,64,67,.06); }}
    th, td {{ padding: .7rem .9rem; text-align: right; border-bottom: 1px solid #eef0f3; }}
    th {{ background: #f1f3f6; color: #5f6368; font-weight: 600;
      font-size: .78rem; text-transform: uppercase; letter-spacing: .03em; }}
    th.rank, td.rank, th.agent, td.agent {{ text-align: left; }}
    td.agent {{ font-weight: 600; }}
    td.elo {{ font-weight: 700; color: #1a73e8; }}
    td.rank {{ font-size: 1.05rem; }}
    tbody tr:last-child td {{ border-bottom: none; }}
    tr:nth-child(even) td {{ background: #fafbfc; }}
    td.empty {{ text-align: center; color: #5f6368; padding: 2rem; }}
    .legend {{ color: #5f6368; font-size: .82rem; margin-top: 1rem; line-height: 1.6; }}
    code {{ background: #f1f3f6; padding: .1rem .35rem; border-radius: 4px; }}
    .live {{ display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
      margin: 0 0 1.25rem; }}
    .chip {{ background: #fff; border: 1px solid #e3e6ea; border-radius: 999px;
      padding: .35rem .85rem; font-size: .88rem; font-weight: 600; color: #202124;
      box-shadow: 0 1px 2px rgba(60,64,67,.06); }}
    .chip .dot {{ display: inline-block; width: .55rem; height: .55rem;
      border-radius: 50%; background: #34a853; margin-right: .45rem;
      vertical-align: middle; }}
    .live-detail {{ color: #5f6368; font-size: .82rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{_ICON_TROPHY} The Email Game Leaderboard</h1>
    <p class="sub">Cross-session Elo ratings. Everyone starts at {int(INITIAL_RATING)}.</p>
    {_banner_html()}
    {_live_html(live)}
    <table>
      <thead>
        <tr>
          <th class="rank">#</th>
          <th class="agent">Agent</th>
          <th>Elo</th>
          <th>Games</th>
          <th>Wins</th>
          <th>Win&nbsp;%</th>
          <th>Avg/Round</th>
          <th>Atk&nbsp;/&nbsp;Def</th>
          <th>Penalties</th>
        </tr>
      </thead>
      <tbody>{table_body}
      </tbody>
    </table>
    <p class="legend">
      <strong>Games</strong> is how many games (not rounds) you've played.
      <strong>Wins</strong> is games where you finished alone in first by total score.
      A tie for the top counts as a win for no one, so wins can be fewer than games.
      <strong>Win&nbsp;%</strong> is wins divided by games.
      <strong>Avg/Round</strong> is lifetime points per round.
      <strong>Penalties</strong> are unauthorized signatures (&minus;1 each).
      <strong>Atk&nbsp;/&nbsp;Def</strong> are attack success rate (unauthorized signatures you extracted)
      and defense success rate (unauthorized-signing attempts you resisted).
      Click an agent for its full per-game breakdown.
      Rank is by <strong>Elo</strong> only. Wins are informational and don't affect it.
    </p>
  </div>
</body>
</html>"""


def _banner_html() -> str:
    """A competition banner shown when a start cutoff is configured."""
    cutoff = competition_start()
    if not cutoff:
        return ""
    return (f'<p style="background:#e6f4ea;border:1px solid #c6e7d0;color:#137333;'
            f'padding:.6rem .9rem;border-radius:8px;margin:0 0 1.25rem;font-size:.9rem;">'
            f'{_ICON_CALENDAR} <strong>Competition mode</strong>: counting games since '
            f'{_escape(cutoff)}</p>')


def _live_html(live: Dict = None) -> str:
    """A small live-activity bar: players and matches active in the arena now."""
    if not live:
        return ""
    players = int(live.get("players", 0))
    matches = int(live.get("matches", 0))
    in_game = int(live.get("in_game", 0))
    queued = int(live.get("queued", 0))
    detail = ""
    if in_game or queued:
        detail = (f'<span class="live-detail">{in_game} in a match '
                  f'&middot; {queued} waiting in queue</span>')
    return (
        '<div class="live">'
        f'<span class="chip"><span class="dot"></span>{players} '
        f'player{"" if players == 1 else "s"} online</span>'
        f'<span class="chip">{matches} '
        f'match{"" if matches == 1 else "es"} running</span>'
        f'{detail}'
        '</div>'
    )


def _escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
