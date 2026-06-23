# Email Game Roadmap

This roadmap tracks useful follow-up work for the Email Game project. The live agent is still the priority while it is actively improving, so dashboard work should remain planned until Papzin asks to build it.

## Telegram Mini App Dashboard

The Telegram Mini App dashboard is a good idea, but it is not urgent while the live agent and Telegram bot workflow are improving. The first version should be read-only.

### Phase 1: Read-Only Dashboard

Show operational visibility without controls:

- leaderboard trend
- score over time
- gap to `#4` and `#1`
- latest match summary
- round success metrics
- signatures requested, received, and submitted
- reminders per match
- coach recommendations
- agent and monitor status

### Phase 2: Telegram Mini App Integration

Integrate the dashboard with Telegram after the read-only version is useful:

- open from Email Game Bot button
- use Telegram WebApp init data
- authorized user only
- no secrets in frontend
- backend reads local state and log summaries

### Phase 3: Safe Controls

Add controls only after read-only dashboarding and authorization are stable:

- start agent only if stopped
- restart only between matches
- stop only between matches
- no force stop
- no arbitrary shell commands
- whitelist only

