# Telegram Control Workflow

This document describes the Telegram control surface for Papzin's Email Game setup.

## Components

### `emailgame`

Live agent tmux session. It should run one instance of:

```bash
./.venv/bin/python scripts/run_custom_agent.py letlhogonolo_fanampe --module my_agent.py --server https://the-email-game.fly.dev
```

Do not start a duplicate live agent.

### `emailgame-monitor`

Telegram monitor tmux session. It handles command responses, notifications, leaderboard polling, and coach alerts.

### `emailgame-coach`

Coach analyzer used by the monitor. It reads local log and state files, then produces summarized recommendations.

## Command Menu

Papzin should type `/` in the Email Game Bot chat and confirm the pop-up command menu appears.

The command registration helper is:

```bash
./.venv/bin/python scripts/set_telegram_commands.py
```

This helper must not print the Telegram bot token.

## Operational Commands

- `/status`: current process and monitor status
- `/logs`: recent redacted log summary
- `/tail`: redacted live log tail
- `/leaderboard`: latest score, rank, and gaps
- `/preflight`: safe local preflight check
- `/version`: running branch, commit, and version context
- `/startagent`: start the agent if stopped
- `/restartagent`: restart the agent only when safe
- `/stopagent`: stop the agent only when safe

## Coach Commands

- `/coach`: summary of current performance signals
- `/recommend`: recommended next operational action
- `/reviewmatch`: recent match review summary
- `/metrics`: counts for rounds, signatures, submissions, reminders, and related signals

## Safety Rules

- Never expose `.env.local`.
- Never expose API keys.
- Never expose the Telegram bot token.
- Never expose watch URL tokens.
- All Telegram output must be redacted before sending.
- Agent restarts should happen only between matches.
- Monitor restarts must not restart the live agent.
- Commands that affect the agent must use a safe whitelist.
- No arbitrary shell execution should be exposed through Telegram.

## Monitor Connected Notification

The monitor should send `Email Game Monitor Connected` once per monitor process start.

Expected behavior:
- one connected notification when a monitor process starts
- no connected notification on every poll loop
- no connected notification on every Telegram command
- persisted notification state in `agent_logs/emailgame-monitor-state.json`

If the connected message repeats, check whether the monitor process is actually restarting or whether the state file is failing to persist.

## Leaderboard And Coach Loop

Recommended loop:

1. Use `/leaderboard` to check rank, score, and gaps.
2. Use `/coach` for the current summary.
3. Use `/recommend` before changing code or restarting anything.
4. Use `/reviewmatch` after recent matches.
5. Use `/metrics` when investigating reminders or missed submissions.

Recent known performance:
- rank around `#5`
- score improved from about `1578` to `1700+`
- gap to `#4` shrinking
- signature submissions now observed
- action reminders still sometimes appear and should remain monitored

