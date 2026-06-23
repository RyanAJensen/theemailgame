# Papzin Email Game Runbook

This runbook documents the current live workflow for Papzin's Email Game agent.

## Current Architecture

### `emailgame`

The `emailgame` tmux session runs the live competition agent:

```bash
./.venv/bin/python scripts/run_custom_agent.py letlhogonolo_fanampe --module my_agent.py --server https://the-email-game.fly.dev
```

The exact agent name must be:

```text
letlhogonolo_fanampe
```

Do not change spelling, casing, or underscores.

### `emailgame-monitor`

The `emailgame-monitor` tmux session runs the Telegram control and notification process:

```bash
./.venv/bin/python scripts/monitor_emailgame_telegram.py
```

Primary Telegram commands:
- `/status`
- `/logs`
- `/tail`
- `/leaderboard`
- `/preflight`
- `/version`
- `/startagent`
- `/restartagent`
- `/stopagent`

The monitor also sends operational alerts for agent status, leaderboard movement, stale logs, disconnects, and coach recommendations.

### `emailgame-coach`

The coach is integrated into the monitor. It reads local logs and state summaries, then produces recommendations without requiring Papzin to read raw logs.

Coach commands:
- `/coach`
- `/recommend`
- `/reviewmatch`
- `/metrics`

Primary local data sources:
- `agent_logs/emailgame-live.log`
- `agent_logs/emailgame-monitor-state.json`
- `agent_logs/emailgame-leaderboard-state.json`
- `agent_logs/emailgame-coach-state.json`

## Model Rules

Allowed models:
- `gpt-4.1-mini`
- `gpt-4.1`

Default model:
- `gpt-4.1-mini`

The agent should use deterministic parsing first. LLM fallback should be used only when deterministic parsing cannot confidently extract the assignment or request target.

## Safety Rules

- Never print `.env.local`.
- Never print API keys.
- Never print the Telegram bot token.
- Never print watch URL tokens.
- Never sign unauthorized requests.
- Never submit stale signed payloads.
- Restart the agent only between matches.
- Use monitor controls only through safe, whitelisted commands.

## Preflight

Run from the repo root:

```bash
bash preflight_papzin_agent.sh
```

Optional key check:

```bash
bash preflight_papzin_agent.sh --check-key
```

Success criteria:
- `.env.local` exists locally.
- `EMAIL_GAME_AGENT_NAME` is exactly `letlhogonolo_fanampe`.
- `EMAIL_GAME_SERVER` is configured locally.
- `my_agent.py` compiles.
- allowed model verification passes when requested.

## Starting the Agent

Preferred helper:

```bash
bash run_papzin_agent.sh
```

In the current long-running setup, the live agent runs inside tmux. Do not start a second agent if one is already running.

Check running processes without exposing secrets:

```bash
pgrep -af 'scripts/run_custom_agent.py letlhogonolo_fanampe|monitor_emailgame_telegram.py'
```

## Stopping and Restarting

Stop or restart the agent only between matches.

Use Telegram controls when possible:
- `/stopagent`
- `/startagent`
- `/restartagent`

Do not force stop the process during an active match unless Papzin explicitly accepts the risk.

The monitor can be restarted independently when monitor code changes. Restarting the monitor must not restart the live agent.

## Mini App Dashboard

A Telegram Mini App dashboard is planned in `ROADMAP.md`, with details in `MINI_APP_PLAN.md`. For now, the Telegram bot remains the command cockpit for status, leaderboard, coach summaries, and safe controls.

## What to Watch

Useful live signals:
- moderator messages for each round
- request target extraction
- signature requests sent
- signed replies received
- submitted signatures
- action completion reminders
- leaderboard score and rank movement

Expected submission logs include:
- `submitted signature for round X`
- `skipped because stale`
- `skipped because unauthorized`
- `missing required signer`

## Key Learnings

- Moderator messages should be processed before non-moderator messages.
- Round 2 and Round 3 fanout was previously missed and has been fixed.
- Signed-message submission needed stronger scanning and confirmation.
- Monitor output needed Telegram-specific formatting and token redaction.
- Leaderboard polling is useful for tracking competitiveness during live play.
- The coach helps identify issues and recommendations without exposing raw log details.

## Current Known Performance

Recent observed state:
- rank around `#5`
- score improved from about `1578` to `1700+`
- gap to `#4` has been shrinking
- signature submissions are now observed
- action reminders still sometimes appear and should remain monitored

## Recommended Operating Loop

1. Check `/status`.
2. Check `/leaderboard`.
3. Use `/coach` or `/recommend` for summarized next actions.
4. Use `/reviewmatch` after recent matches.
5. Use `/metrics` when investigating repeated reminders or missed submissions.
6. Restart only the monitor after monitor-only code changes.
7. Restart the agent only between matches.
