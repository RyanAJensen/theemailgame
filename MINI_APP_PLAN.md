# Telegram Mini App Plan

This is a plan only. Do not build the mini app until Papzin asks.

The roadmap tracking item is in `ROADMAP.md` under `Telegram Mini App Dashboard`.

## Goal

Create a safe Telegram Mini App that gives Papzin a compact operational dashboard for the Email Game agent, monitor, leaderboard, and coach recommendations.

The first version should be read-only.

Competition context the dashboard should preserve:

- Scoring is `+1` for each valid signature collected and submitted to the moderator.
- Scoring is `+1` for each authorized signature provided.
- Scoring is `-1` for signing for an agent we were not authorized to sign for.
- Build-week leaderboard checks use `/leaderboard/testing`.
- The official competition runs June 27, 11 AM-5 PM ET.
- House bots are deployed by organizers for build-week matches.

## Phase 1: Read-Only Dashboard

Build a local dashboard that shows:

- leaderboard chart
- score trend
- gap to `#4` and `#1`
- latest match summary
- round success table
- submissions vs reminders
- coach recommendations
- agent and monitor status
- build-week score, rank, and gap context

Suggested data sources:

- `agent_logs/emailgame-monitor-state.json`
- `agent_logs/emailgame-leaderboard-state.json`
- `agent_logs/emailgame-coach-state.json`
- `agent_logs/emailgame-live.log`

The dashboard should read summarized local state. It should not expose raw secrets or raw watch URLs.

## Phase 2: Telegram WebApp Integration

Add Telegram Mini App integration after the read-only dashboard is useful locally.

Requirements:

- open from an Email Game Bot button
- use Telegram Mini App init data
- validate authorized user access
- keep all secrets out of the frontend
- keep the Telegram bot token out of the frontend
- keep API keys out of the frontend
- keep watch tokens out of the frontend
- backend reads local state JSON files and log summaries
- no direct shell execution from the frontend

## Phase 3: Safe Controls

Only add controls after the read-only dashboard and auth checks are stable.

Allowed controls:

- start the agent if stopped
- restart the agent only between matches
- stop the agent only between matches

Disallowed controls:

- force stop during a match
- arbitrary shell commands
- arbitrary file reads
- secret display
- raw log display without redaction

All controls must go through a safe backend whitelist.

## Security Rules

- read-only first
- no API keys in frontend
- no Telegram bot token in frontend
- no watch tokens in frontend
- no arbitrary shell execution
- authorized user only
- all commands must go through a safe whitelist
- redact logs before returning them to the UI
- prefer summarized metrics over raw log lines
- keep Discord-shared keys and credentials out of all UI output

## Implementation Recommendation

Use a simple local backend first:

- FastAPI or another lightweight localhost HTTP server
- local-only binding by default
- optional Cloudflare Tunnel only if Papzin needs remote access
- frontend can be Vite/React later
- backend reads JSON state files and redacted log summaries

Suggested backend endpoints:

- `GET /api/status`
- `GET /api/leaderboard`
- `GET /api/coach`
- `GET /api/matches/latest`
- `GET /api/metrics`

Safe-control endpoints should wait until Phase 3 and should require explicit authorization.

## Strategy Notes

- Do not over-optimize against only house bots.
- Use house bots as useful test coverage for signature request, signing, and submission flow.
- Expect real participants to behave differently from house bots.
- Continue surfacing score, rank, and gaps from the Telegram coach instead of inferring competitiveness from one match.

## Recommended Build Order

1. Define a small JSON summary schema from existing state files.
2. Build local read-only FastAPI endpoints.
3. Add a simple dashboard UI.
4. Validate redaction and authorization boundaries.
5. Add Telegram WebApp launch.
6. Add safe controls only after read-only usage is stable.
