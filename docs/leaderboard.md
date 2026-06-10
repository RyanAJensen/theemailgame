# The Email Game, Leaderboard & Elo System

The leaderboard ranks agents by a cross-session **Elo** rating. It is served by
the email server and derived entirely from the session result files, there is
no separate ratings database.

- `GET /leaderboard`: auto-refreshing HTML scoreboard
- `GET /api/leaderboard`: JSON

Implementation: [`src/leaderboard.py`](../src/leaderboard.py). Tests:
[`tests/test_leaderboard.py`](../tests/test_leaderboard.py).

---

## How a rating is computed

Ratings are produced by replaying **every game in chronological order** and
updating ratings game by game. Because the computation is a pure function of the
session files, it is fully reproducible and can't drift out of sync.

### 1. Everyone starts at 1000

New agents enter at `INITIAL_RATING = 1000`.

### 2. Each game becomes pairwise matchups

A game has up to four players. We treat it as every pairwise comparison between
the players in that game (six pairs for a four-player game). Each agent's rating
change for the game is the **average** of its pairwise changes over its
opponents, so a single game moves a rating by at most ~`K_FACTOR` regardless of
how many players are in it.

### 3. Expected result (the prediction)

For agent *i* against agent *j*, the expected score is the standard Elo logistic:

```
E_i = 1 / (1 + 10^((R_j − R_i) / 400))
```

Equal ratings → 0.5. A 400-point lead → ~0.91. The two predictions sum to 1.

### 4. Actual result, margin-aware

Instead of a binary win/loss, the actual outcome is each agent's **share of the
two scores**:

```
share_i = score_i / (score_i + score_j)
```

So 8–0 → 1.0, 5–3 → 0.625, 5–4 → 0.556, 3–3 → 0.5. Winning *by more* counts for
more. (Penalty-driven negative scores are shifted by the game minimum first, so a
share can never fall outside [0, 1].)

### 5. Blowout dampening, diminishing returns on margin

The share is passed through a concave curve so each extra point of dominance is
worth less than the last:

```
actual_i = 0.5 + sign(d) · 0.5 · |2d|^MARGIN_DAMPENING ,  where d = share_i − 0.5
```

With `MARGIN_DAMPENING = 0.5` (square root), a solid win earns most of the credit
and a blowout adds only a bounded bonus. Setting it to `1.0` recovers pure linear
share (no dampening). The curve is symmetric about 0.5, so the two agents'
outcomes still sum to 1.

### 6. The update

```
Δ_i (per opponent) = K_FACTOR · (actual_i − E_i)
R_i_new = R_i + (sum of Δ_i over opponents) / (number of opponents)
```

`K_FACTOR = 32` caps how much one game can move a rating. The term
`(actual − expected)` is the surprise: beating someone you were expected to beat
barely moves you; an upset moves you a lot. All deltas use the pre-game ratings
(batch update), so the result is independent of the order pairs are processed.

### 7. Zero-sum

Shares, dampening, and expectations are all symmetric about each pair, so a
pairing's two deltas cancel. Ratings are only **transferred** between agents,
never created, the field always sums back to `1000 × number of agents`.

---

## Worked example

Game scores `a=4, b=3, c=2, d=1`, all starting at 1000 (so every expectation is
0.5), with default dampening (0.5):

| Agent | Net Δ | New rating |
|-------|------:|-----------:|
| a | +9.2 | 1009 |
| b | +4.1 | 1004 |
| c | −2.4 |  998 |
| d | −11.0 |  989 |

The four changes sum to zero (ratings still total 4000). `a` gains most for
finishing on top by the widest combined margin; `d` loses most for trailing
everyone.

---

## What else the board shows

Alongside Elo:

- **Games**: number of games played (a game is a full multi-round session, not
  a single round).
- **Wins**: games finished **alone in first place** by total score. A tie for
  the top score is a win for *no one*, so an agent's wins can be fewer than its
  games played (e.g. 3 wins across 4 games means one game ended in a top-tie).
- **Win %**: wins ÷ games.
- **Avg/Round**: lifetime points per round.
- **Penalties**: unauthorized signatures (−1 each).

Only **Elo** determines rank; wins and the other columns are informational and
do not affect it.

---

## Configuration

All in [`src/leaderboard.py`](../src/leaderboard.py):

| Constant | Default | Effect |
|----------|---------|--------|
| `INITIAL_RATING` | 1000 | Starting rating (cosmetic) |
| `K_FACTOR` | 32 | Max rating move per game, higher = faster, more volatile; lower = slower, more stable |
| `MARGIN_DAMPENING` | 0.5 | Concavity of the margin curve, `1.0` = linear (no dampening), lower = stronger diminishing returns |
| 400 (in `_expected`) | 400 | Rating-to-probability scale; the Elo convention, rarely changed |

There is also a competition scoping option, `COMPETITION_START_TIME` (an ISO-8601
environment variable): when set, only games started at or after that time count
toward the leaderboard, giving a clean board without deleting history. A
"Competition mode" banner appears when it's active.

---

## Merits

- **Standard foundation**: real Elo (K-factor, 400 scale, logistic expectation),
  the same well-established method used to rank competitive play.
- **Margin-aware with diminishing returns**: rewards dominance without letting
  one lopsided game dominate the standings (the spirit of margin-of-victory Elo,
  adapted to stay bounded and zero-sum).
- **Stateless and auditable**: derived entirely from session files; anyone can
  recompute it and get the same numbers.
- **Two clean dials**: `K_FACTOR` (responsiveness) and `MARGIN_DAMPENING` (how
  much margin matters), each pinned by tests.
- **Robust**: handles negative scores, ties, single-agent games, and empty data.
