# Auto-tuning pipeline

A supervisor (`controller.py`) runs the agent, watches the official per-game
stats, and every N games applies safe **config-only** strategy changes, then
restarts the agent **gracefully** (no forfeit) to pick them up.

## Run it

```bash
cd /Users/calvinandoh/Desktop/theemailgame
SSL_CERT_FILE='/Library/Frameworks/Python.framework/Versions/3.14/lib/python3.14/site-packages/certifi/cacert.pem' \
OPENAI_API_KEY="sk-...issued..." OPENAI_BASE_URL="https://the-email-game-llm.fly.dev" \
python3 controller.py calvin_andoh --module my_agent.py \
    --server https://the-email-game.fly.dev --tune-every 10 --poll 30
```

Stop with `Ctrl+C` — the controller stops the agent gracefully (at a game
boundary) before exiting. The agent's stdout goes to `live_agent.log`; the
controller's decisions go to `controller.log`.

## How it works

- **Stats**: the controller parses the agent's personal view token from its
  startup banner and polls `GET /api/agent/<name>?board=competition`.
- **Tuning** (`decide_config_changes` in `controller.py`): every `--tune-every`
  new games it inspects that window and edits `strategy_config.json`. Current
  rules:
  - offense landed 0 attacks over the window → `enable_offense: false`
  - collection low but identity hint already on → **MANUAL REVIEW** alert (no
    auto-fix; surfaced in `controller.log`)
  - got fooled (signed unauthorized) → **MANUAL REVIEW** alert (defense is a code
    matter, not a config knob)
  - proven decliners (from `opponent_profiles.json`) → added to `offense_blocklist`
- **Graceful restart**: controller writes a `RESTART_REQUESTED` sentinel; the
  agent checks it at each game-over (the penalty-free window) and exits itself;
  the controller deletes the sentinel and relaunches. SIGINT/SIGKILL fallback if
  a clean boundary isn't reached in ~4 min.
- **Memory**: `opponent_profiles.json` (the Adaptive Learner) is saved every game
  and reloaded on launch, so learning survives restarts.

## strategy_config.json knobs

| key | meaning | default |
|-----|---------|---------|
| `include_prev_message_hint` | quote previous-round message so opponents can resolve us in round 2+ (fixes round-2/3 collection) | `true` |
| `enable_offense` | run the budget-free Cautious-Saboteur asks | `true` |
| `offense_give_up_after` | stop targeting an agent after this many failed asks | `2` |
| `offense_blocklist` | agents to never attack | `[]` |
| `enable_fuzzy_poisoning` | speculative round-1 poisoning (weak EV) | `false` |

The agent runs fine **without** the controller — absent a config file it uses
these defaults, and absent a sentinel it never auto-exits.

## Safety notes / limitations

- Changes are **data only**; the controller never edits agent code, so it cannot
  crash the agent or introduce a signing bug.
- Anything risky (defense breaches, persistent collection failure) is **flagged
  for manual review**, not auto-"fixed".
- The graceful-restart and token-parsing paths were validated by code review +
  the rules engine and agent behavior were unit/integration tested locally, but
  the full restart cycle has **not** been exercised against a live multi-game
  server (the competition had ended at build time). Watch `controller.log` on the
  first live run.
