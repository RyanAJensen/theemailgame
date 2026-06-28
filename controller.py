#!/usr/bin/env python3
"""
Auto-tuning supervisor for the Email Game agent.

Runs the agent as a subprocess and, every N completed games, pulls the official
per-game stats, applies a small set of safe rules to strategy_config.json, then
restarts the agent GRACEFULLY (it exits itself at a game boundary, so no forfeit)
to pick up the new config.

Design notes / safety:
  * Strategy changes are CONFIG ONLY (data flags the tested agent already reads).
    The controller never rewrites agent code, so it cannot crash the agent or
    introduce a signing bug.
  * Graceful restart is cooperative: the controller drops a RESTART_REQUESTED
    sentinel; the agent checks it at each game-over (the penalty-free window) and
    exits itself. The controller then deletes the sentinel and relaunches. A
    SIGINT/SIGKILL fallback covers the rare case the agent never reaches a clean
    boundary.
  * opponent_profiles.json (Adaptive Learner memory) is persisted by the agent so
    it survives these restarts.

Usage:
  SSL_CERT_FILE=... python3 controller.py calvin_andoh \
      --module my_agent.py --server https://the-email-game.fly.dev \
      --tune-every 10 --poll 30

The token for the stats API is parsed automatically from the agent's startup
banner, so no token argument is needed.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "strategy_config.json"
PROFILES_PATH = ROOT / "opponent_profiles.json"
RESTART_SENTINEL = ROOT / "RESTART_REQUESTED"
CONTROLLER_LOG = ROOT / "controller.log"

TOKEN_RE = re.compile(r"token=([A-Za-z0-9._-]+)")


def log(msg: str) -> None:
    line = f"[controller {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(CONTROLLER_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def read_profiles() -> dict:
    try:
        return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Stats API
# ---------------------------------------------------------------------------

def fetch_agent_stats(server: str, agent: str, token: str) -> dict | None:
    """Return the parsed /api/agent report, or None on any failure."""
    url = f"{server}/api/agent/{agent}?board=competition&cb={int(time.time())}&token={token}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("success"):
            return data["report"]
    except Exception as exc:
        log(f"stats fetch failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# Rules engine - maps recent-game stats to safe config changes
# ---------------------------------------------------------------------------

def decide_config_changes(window_games: list, cfg: dict, profiles: dict) -> tuple[dict, list]:
    """Return (new_config, list_of_human_readable_decisions).

    Only data flags are changed. Anything the rules can't safely fix is surfaced
    as a MANUAL REVIEW note rather than acted on.
    """
    new_cfg = dict(cfg)
    notes = []
    played = [g for g in window_games if not g.get("abandoned")]
    if not played:
        return new_cfg, ["no completed games in window - no change"]

    n = len(played)
    attacks = sum(g.get("attacks_landed", 0) for g in played)
    fooled = sum(g.get("times_fooled", 0) for g in played)
    avg_collect = sum(g.get("authorized_collected", 0) for g in played) / n
    avg_signs = sum(g.get("authorized_signs", 0) for g in played) / n
    abandoned = len(window_games) - n

    notes.append(f"window: {n} played (+{abandoned} abandoned) | "
                 f"attacks_landed={attacks} times_fooled={fooled} "
                 f"avg_collected={avg_collect:.2f}/6 avg_signs={avg_signs:.2f}/6")

    # Rule 1: defense breach is serious and not auto-fixable by config -> alert.
    if fooled > 0:
        notes.append("⚠️ MANUAL REVIEW: got fooled (signed unauthorized). "
                     "Defense logic needs a code look, not a config tweak.")

    # Rule 2: collection lagging despite the identity hint -> alert (can't force
    # opponents to resolve us); make sure the hint is at least enabled.
    if avg_collect < 5.0:
        if not new_cfg.get("include_prev_message_hint", True):
            new_cfg["include_prev_message_hint"] = True
            notes.append("collection low -> enabled include_prev_message_hint")
        else:
            notes.append("⚠️ MANUAL REVIEW: collection low even WITH the identity "
                         "hint - opponents may not be resolving us; consider a "
                         "richer hint (quote round-1 message too).")

    # Rule 3: offense not landing -> stop the noise (it's free but pointless here).
    if new_cfg.get("enable_offense", True) and attacks == 0 and n >= 5:
        new_cfg["enable_offense"] = False
        notes.append(f"offense landed 0 in {n} games -> disabled enable_offense")

    # Rule 4: persist proven decliners into the blocklist (visible + survives a
    # lost profiles file) using the agent's own Adaptive Learner counts.
    give_up = new_cfg.get("offense_give_up_after", 2)
    block = set(new_cfg.get("offense_blocklist", []))
    for aid, p in profiles.items():
        if p.get("offense_attempts", 0) >= give_up and p.get("times_they_signed_unauthorized_for_us", 0) == 0:
            block.add(aid)
    if set(new_cfg.get("offense_blocklist", [])) != block:
        new_cfg["offense_blocklist"] = sorted(block)
        notes.append(f"offense_blocklist -> {sorted(block)}")

    return new_cfg, notes


# ---------------------------------------------------------------------------
# Agent process management
# ---------------------------------------------------------------------------

def launch_agent(agent: str, module: str, server: str, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [sys.executable, "scripts/run_custom_agent.py", agent,
           "--module", module, "--server", server]
    log(f"launching: {' '.join(cmd)}  (-> {log_path.name})")
    fh = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, env=env)
    proc._log_fh = fh  # keep handle alive
    return proc


def wait_for_token(log_path: Path, timeout: float = 40.0) -> str | None:
    """Parse the personal view token from the agent's startup banner."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            m = TOKEN_RE.search(text)
            if m:
                return m.group(1)
        except Exception:
            pass
        time.sleep(1)
    return None


def graceful_restart(proc: subprocess.Popen) -> None:
    """Ask the agent to exit at its next game boundary, with signal fallbacks."""
    log("requesting graceful stop (RESTART_REQUESTED sentinel)...")
    RESTART_SENTINEL.write_text("1", encoding="utf-8")
    # Cooperative exit: agent leaves at next game-over (a game is ~3 min).
    for _ in range(240):  # up to ~240s
        if proc.poll() is not None:
            log("agent exited cleanly between games.")
            break
        time.sleep(1)
    else:
        log("no clean exit in time - sending SIGINT (between-games fallback).")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            log("SIGINT ignored - SIGKILL.")
            proc.kill()
    # Critical: clear the sentinel BEFORE relaunch or the new process would exit
    # at its very first game-over.
    try:
        RESTART_SENTINEL.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-tuning supervisor for the Email Game agent.")
    ap.add_argument("agent", help="your exact agent name, e.g. calvin_andoh")
    ap.add_argument("--module", default="my_agent.py")
    ap.add_argument("--server", default="https://the-email-game.fly.dev")
    ap.add_argument("--tune-every", type=int, default=10, help="re-tune after this many new games")
    ap.add_argument("--poll", type=int, default=30, help="seconds between stats polls")
    ap.add_argument("--log", default="live_agent.log", help="agent stdout log file")
    args = ap.parse_args()

    log_path = ROOT / args.log
    proc = launch_agent(args.agent, args.module, args.server, log_path)
    token = wait_for_token(log_path)
    if not token:
        log("WARNING: could not parse a stats token; will keep retrying after first poll.")

    # Establish the games-played watermark so we count `tune-every` fresh games.
    baseline = None
    while baseline is None:
        if token:
            rep = fetch_agent_stats(args.server, args.agent, token)
            if rep:
                baseline = rep["summary"]["games"]
                log(f"baseline games={baseline}, rank={rep['summary'].get('rank')}")
                break
        if proc.poll() is not None:
            log("agent died before first stats read; exiting.")
            return
        time.sleep(args.poll)

    def shutdown(*_):
        log("controller interrupted - stopping agent gracefully and exiting.")
        graceful_restart(proc)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    last_tuned_at = baseline
    while True:
        time.sleep(args.poll)

        if proc.poll() is not None:
            log("agent process exited unexpectedly - relaunching.")
            proc = launch_agent(args.agent, args.module, args.server, log_path)
            token = wait_for_token(log_path) or token
            continue

        rep = fetch_agent_stats(args.server, args.agent, token)
        if not rep:
            token = wait_for_token(log_path, timeout=2) or token  # maybe token rotated
            continue

        total = rep["summary"]["games"]
        rank = rep["summary"].get("rank")
        if total - last_tuned_at < args.tune_every:
            log(f"games={total} (rank {rank}); {args.tune_every - (total - last_tuned_at)} to next tune")
            continue

        # Re-tune on the games played since the last tune.
        window = rep["games"][: (total - last_tuned_at)]  # API returns newest-first
        cfg = read_config()
        new_cfg, notes = decide_config_changes(window, cfg, read_profiles())
        log(f"=== TUNE at games={total} (rank {rank}) ===")
        for note in notes:
            log("  " + note)

        if new_cfg != cfg:
            write_config(new_cfg)
            log(f"config updated -> {new_cfg}")
            graceful_restart(proc)
            proc = launch_agent(args.agent, args.module, args.server, log_path)
            token = wait_for_token(log_path) or token
        else:
            log("no config change warranted; continuing without restart.")
        last_tuned_at = total


if __name__ == "__main__":
    main()
