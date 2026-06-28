# My Contributions — The Email Game

> **Attribution:** The Email Game framework (game server, scoring engine, match
> runtime, leaderboard) is the work of the upstream author
> ([RyanAJensen/theemailgame](https://github.com/RyanAJensen/theemailgame)). This
> is a fork. **My contribution is the competing agent and the auto-tuning
> supervisor built on top of that framework** — the files listed below.

## What this is

The Email Game is a multiplayer benchmark where LLM agents exchange
cryptographically signed emails: each round you must collect signatures on your
assigned message, sign for the agents you're authorized to, and avoid being
socially engineered into signing for anyone you're *not* (which costs points).
I built an autonomous agent to compete in it and a supervisor that tunes the
agent's strategy from live results.

## Files I wrote

| File | What it is |
|------|-----------|
| [`my_agent.py`](my_agent.py) | The competing agent — a deterministic-first strategy with four layers (see below). |
| [`controller.py`](controller.py) | An auto-tuning supervisor that runs the agent, reads official per-game stats, and adjusts strategy between games with no forfeit. |
| [`strategy_config.json`](strategy_config.json) | Runtime-tunable strategy knobs the agent reads. |
| [`PIPELINE.md`](PIPELINE.md) | Design + usage of the auto-tuning pipeline. |

## Agent design (`my_agent.py`)

A **deterministic-first** design: the LLM is used only for one narrow, code-gated
task (resolving fuzzy identity descriptions); every scoring-critical decision is
plain code, so the agent can't be talked into a penalty.

1. **Flawless Executor** — parses moderator instructions and fires signature
   requests immediately in code (no LLM latency), with a retry safety net for a
   round-start race condition I found in the logs.
2. **Impregnable Fortress (defense)** — whether to sign is gated entirely on the
   server-authoritative `from` field + the moderator's actual authorization list;
   claims made in an email *body* are never trusted. Round-2+ fuzzy descriptions
   are resolved against my own observed message history, and the LLM's answer is
   only accepted if it exactly matches an agent I have history for — neutralizing
   prompt-injection in the descriptions.
3. **Cautious Saboteur (offense)** — since scoring awards the submitter a point
   for any valid signature regardless of the signer's authorization, the agent
   opportunistically (and budget-free) asks unauthorized agents to sign, turning
   ties into wins.
4. **Adaptive Learner** — per-opponent behavior persists across games (and across
   restarts, via disk), so targeting improves over a session.

## Auto-tuning pipeline (`controller.py`)

A supervisor process that:
- launches the agent and parses its stats token automatically,
- polls the official `/api/agent` endpoint and, every N games, applies a
  rules-based, **config-only** tuning pass (it never rewrites code, so it can't
  crash the agent or introduce a penalty), and
- restarts the agent **gracefully** via a cooperative-shutdown handshake — the
  agent exits itself at a game boundary (the penalty-free window), so re-tuning
  never forfeits a match.

Adaptive-Learner memory is persisted to disk so learning survives the restarts.

## Process highlights (what I'd talk about in an interview)

- **Data-driven iteration:** I pulled the official competition stats via the
  server's JSON API and diagnosed concrete leaks from match transcripts — e.g.
  round-2/3 collection was failing because opponents couldn't resolve *my* fuzzy
  identity, so I made my requests quote my previous-round message (a trick a
  higher-ranked agent was using). Defensively, after an early cluster of
  penalties, the code-gated signing logic took penalties to zero across the rest
  of the run.
- **Result:** finished **rank 5 of 12** on the official leaderboard, up from rank
  9, after these fixes.
- **Engineering judgment:** chose config-driven auto-tuning over autonomous code
  generation specifically because a live, money-on-the-line agent shouldn't be
  exposed to unvetted machine-written code; risky conditions are surfaced for
  human review instead of auto-"fixed".

## Running it

See [`PIPELINE.md`](PIPELINE.md) for the supervisor, and the upstream
[`README.md`](README.md) for the framework/setup.
