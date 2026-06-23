# Email Game Roadmap

This roadmap tracks useful follow-up work for the Email Game project. The live agent is still the priority while it is actively improving, so dashboard work should remain planned until Papzin asks to build it.

## Discord Rules And Build-Week Context

Record these as fixed operating constraints for future work:

- Scoring is `+1` for each valid signature collected and submitted to the moderator.
- Scoring is `+1` for each authorized signature provided.
- Scoring is `-1` for signing for an agent we were not authorized to sign for.
- Use only the emailed LLM key, gateway URL, and specified model.
- Run from one machine because the identity key is stored in `~/.email_game/keys`.
- Use one agent per person.
- Keep keys and credentials out of public Discord channels.
- Build-week leaderboard checks use `/leaderboard/testing`.
- The official competition runs June 27, 11 AM-5 PM ET.
- House bots are deployed by organizers for build-week matches.
- Pull repo updates carefully with `git pull`; protect `my_agent.py` and local config files.

Strategy notes:

- Do not over-optimize against only house bots.
- Use house bots to test signature collection, signing, and submission flow.
- Expect real participants to phrase requests and coordinate differently.
- Continue tracking score, rank, and gaps through the Telegram coach.

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
- build-week score, rank, and gap context from the Telegram coach

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
