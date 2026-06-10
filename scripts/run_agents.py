#!/usr/bin/env python3
"""Launch one or more base agents against a server, from a single terminal.

Useful for the host side of a hosted game: start your own agent plus any filler
agents in one command instead of juggling terminals. Ctrl+C stops them all.

Examples:
    # your agent + two fillers, all pointed at the live server
    python scripts/run_agents.py --server https://the-email-game.fly.dev ryan filler1 filler2

    # give them a specific model
    python scripts/run_agents.py --server https://the-email-game.fly.dev a b c --model gpt-4o-mini
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

# Keep emoji/log output safe when stdout is redirected on non-UTF-8 consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description="Run multiple base agents in one terminal")
    ap.add_argument("names", nargs="+", help="agent ids to launch (one word each)")
    ap.add_argument("--server", default="http://localhost:8000", help="server URL")
    ap.add_argument("--model", default=None, help="OpenAI model for all agents")
    ap.add_argument("--temperature", default=None, help="temperature for all agents")
    args = ap.parse_args()

    procs = []
    for name in args.names:
        cmd = [sys.executable, "-m", "src.base_agent", name, name.title(), "--server", args.server]
        if args.model:
            cmd += ["--model", args.model]
        if args.temperature:
            cmd += ["--temperature", args.temperature]
        procs.append(subprocess.Popen(cmd, cwd=PROJECT_ROOT))
        print(f"launched {name}")
        time.sleep(2)

    print(f"\n{len(procs)} agent(s) running against {args.server}. Press Ctrl+C to stop them all.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping agents...")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("done")


if __name__ == "__main__":
    main()
