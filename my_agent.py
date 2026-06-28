"""
The Email Game - Custom Agent

A deterministic-first agent implementing four layered strategies:

1. Flawless Executor - moderator instructions are parsed with plain string/regex
   code (not an LLM), and signature requests fire immediately on parse.
2. Impregnable Fortress - whether to sign for someone is gated entirely on the
   moderator's actual lists (explicit names, or round 2+ fuzzy descriptions
   resolved against our own observed message history). Claims made in an email
   BODY are never trusted for authorization; the server-authoritative `from`
   field is the only identity signal used.
3. Cautious Saboteur - since scoring gives the submitter +1 for any
   cryptographically valid signature regardless of the signer's authorization
   (the -1 penalty falls on the signer, not us), we opportunistically ask
   agents who are NOT actually authorized to sign for us (i.e. not in our
   request_list - by construction the request_list is exactly the set of
   agents authorized to sign for us, so anyone else is a clean target).
4. Adaptive Learner - opponent behavior persists across games for the life of
   this process (the whole ladder session), informing future offense targeting.

The LLM is used for exactly two narrow, code-gated tasks: resolving a fuzzy
description against our own history, and drafting the wording of an outbound
request. It never decides whether to sign anything - that decision is always
a plain membership check against moderator-derived state.

IMPORTANT - the files in data/ are SAMPLES for local testing only. The live
competition server uses different, PRIVATE data; fuzzy descriptions are
resolved here purely by reasoning over message history actually received,
never by reading any shipped data file.
"""

import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parents[0]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.base_agent import BaseAgent

# ----------------------------------------------------------------------------
# Auto-tuning pipeline integration (see controller.py)
# ----------------------------------------------------------------------------
# These three files let an external supervisor tune strategy between games
# without editing code:
#   strategy_config.json   - tunable knobs, re-read at every game boundary
#   opponent_profiles.json - cross-game Adaptive Learner memory, persisted so it
#                            survives the controller's periodic restarts
#   RESTART_REQUESTED       - sentinel; when present, the agent exits itself at
#                            the next game-over (the penalty-free window) so the
#                            controller can re-tune and relaunch with no forfeit
CONFIG_PATH = PROJECT_ROOT / "strategy_config.json"
PROFILES_PATH = PROJECT_ROOT / "opponent_profiles.json"
RESTART_SENTINEL = PROJECT_ROOT / "RESTART_REQUESTED"

# Defaults used when strategy_config.json is absent or a key is missing. The
# agent runs identically to the hand-tuned version with no config file present,
# so it works standalone (no controller required).
DEFAULT_CONFIG = {
    # Round-2+ collection fix: quote our previous-round message so recipients can
    # resolve the fuzzy description that refers to us. High value; keep on.
    "include_prev_message_hint": True,
    # Cautious Saboteur offense (budget-free static template).
    "enable_offense": True,
    # Lifetime unauthorized asks with zero successes before we give up on a target.
    "offense_give_up_after": 2,
    # Agents we will never bother attacking (controller can populate this with
    # proven decliners so the giving-up persists across restarts).
    "offense_blocklist": [],
    # Speculative round-1 "poison the well" tactic (weakest-EV; off by default).
    "enable_fuzzy_poisoning": False,
}


def load_strategy_config() -> Dict:
    """Read strategy_config.json over the defaults. Never raises - a missing or
    malformed file just yields the defaults so the agent always starts."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"[config] failed to read {CONFIG_PATH.name}, using defaults: {exc}")
    return cfg


# Static, budget-free offense request bodies. Real opponents are heavily
# defensive (the live log showed "Cannot sign this request" 129x), so paying for
# an LLM-drafted persuasion per round is poor value; a clean rotating template
# costs nothing and is no less effective against agents that gate on their lists.
OFFENSE_TEMPLATES = [
    "Hey {target}, quick favor when you have a sec? Please sign this message for me: {message}",
    "Hi {target}! Could you help me out with a quick signature? Please sign this message for me: {message}",
    "{target}, mind doing me a small favor? Please sign this message for me: {message}",
]

REQUEST_LIST_ANCHOR = "You must REQUEST signatures from these agents:"
SIGNING_LIST_ANCHOR = "You are AUTHORIZED to sign messages for these agents:"
ASSIGNED_MESSAGE_ANCHOR = 'You must get signatures for this EXACT message: "'
FUZZY_SUFFIX = "(from last round; their message this round may be different)"
ROUND_PATTERN = re.compile(r"\*\*ROUND\s+(\d+)\*\*", re.IGNORECASE)

SIGNING_REQUEST_PATTERNS = [
    re.compile(r"please sign (?:this )?message for me:\s*", re.IGNORECASE),
    re.compile(r"sign this(?:\s+message)?(?:\s+for me)?:\s*", re.IGNORECASE),
    re.compile(r"sign the following(?:\s+message)?:\s*", re.IGNORECASE),
]


def _split_top_level_commas(text: str) -> List[str]:
    """Split on commas, but not commas nested inside parentheses."""
    parts, current, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _extract_message_to_sign(body: str) -> Optional[str]:
    """Pull the verbatim message text out of an inbound signature request."""
    for line in body.splitlines():
        for pattern in SIGNING_REQUEST_PATTERNS:
            match = pattern.search(line)
            if match:
                candidate = line[match.end():].strip()
                if len(candidate) > 1 and candidate.startswith('"') and candidate.endswith('"'):
                    candidate = candidate[1:-1]
                return candidate.strip()
    return None


class CustomAgent(BaseAgent):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Strategy knobs (re-read at each game boundary so a controller restart
        # isn't strictly required for a change to take effect).
        self.config = load_strategy_config()
        # Cross-game Adaptive Learner memory. Loaded from disk so it survives the
        # controller's periodic restarts (an in-memory-only dict would reset to
        # empty every relaunch, defeating the learning).
        self.opponent_profiles: Dict[str, dict] = self._load_profiles()
        self._reset_game_state()
        self._reset_round_state()
        self._last_processed_round = 0
        print(f"[{self.agent_id}] strategy config: {self.config}")

    # ------------------------------------------------------------------
    # Persistence (Adaptive Learner survives restarts)
    # ------------------------------------------------------------------

    def _load_profiles(self) -> Dict[str, dict]:
        try:
            if PROFILES_PATH.exists():
                return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[{self.agent_id}] could not load opponent_profiles: {exc}")
        return {}

    def _save_profiles(self) -> None:
        try:
            PROFILES_PATH.write_text(json.dumps(self.opponent_profiles, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[{self.agent_id}] could not save opponent_profiles: {exc}")

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _reset_game_state(self) -> None:
        self.message_history_by_agent: Dict[str, List[str]] = {}
        self.known_agents_this_game: Set[str] = set()
        self._profile_counted_this_game: Set[str] = set()
        self._poisoned_this_game = False
        # Our own assigned message per round this game. In round 2+ other agents
        # see US as a fuzzy description and must resolve it to sign for us; we
        # help them by quoting our PREVIOUS round's message in our requests.
        self.my_messages_by_round: Dict[int, str] = {}

    def _reset_round_state(self) -> None:
        # signatures_received/requested_from track THIS round's collection
        # progress and must clear every round, not just every game - a stale
        # requested_from from a prior round would otherwise block the
        # "send immediately" guard below from ever firing again.
        self.signatures_received: Set[str] = set()
        self.requested_from: Set[str] = set()
        self.my_assigned_message: Optional[str] = None
        self.request_list: List[str] = []
        self.resolved_signing_list: Set[str] = set()
        self.unresolved_fuzzy_raw: List[str] = []
        self.offense_attempted_this_round = False
        self.batches_seen_this_round = 0
        self.requests_retried_this_round = False

    def on_new_game(self) -> None:
        """Called at the start of each new game. Reset all per-game and
        per-round state; cross-game opponent_profiles is intentionally left
        untouched so the agent keeps learning across the whole ladder session.
        Re-read the strategy config so a controller edit applies even without a
        full restart.
        """
        self.config = load_strategy_config()
        self._reset_game_state()
        self._reset_round_state()
        self._last_processed_round = 0

    def _handle_message_batch(self, messages: List[Dict]) -> None:
        """Wrap the base handler to add cooperative shutdown. The base flips
        in_game -> False on a moderator 'game over'. If we just made that
        transition AND a restart was requested, persist learning and exit now -
        this is the penalty-free between-games window, so the controller can
        re-tune and relaunch without forfeiting a match.
        """
        was_in_game = self.in_game
        super()._handle_message_batch(messages)
        if was_in_game and not self.in_game:
            self._save_profiles()  # checkpoint Adaptive Learner memory each game
            if RESTART_SENTINEL.exists():
                print(f"[{self.agent_id}] 🔄 restart requested - exiting cleanly "
                      f"between games for re-tuning.")
                sys.stdout.flush()
                os._exit(0)

    def _ensure_profile(self, agent_id: str) -> None:
        if agent_id in (self.agent_id, self.moderator_agent):
            return
        profile = self.opponent_profiles.setdefault(agent_id, {
            "games_played": 0,
            "unauthorized_requests_from_them": 0,
            "times_they_signed_unauthorized_for_us": 0,
            "offense_attempts": 0,
        })
        # Backfill for profiles created before this field existed (same process).
        profile.setdefault("offense_attempts", 0)
        if agent_id not in self._profile_counted_this_game:
            profile["games_played"] += 1
            self._profile_counted_this_game.add(agent_id)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def on_message_batch(self, messages: List[Dict]) -> None:
        self.batches_seen_this_round += 1

        moderator_messages = [m for m in messages if m.get("from") == self.moderator_agent]
        other_messages = [
            m for m in messages
            if m.get("from") not in (self.moderator_agent, "system_reminder")
        ]

        for msg in moderator_messages:
            self._handle_moderator_message(msg)

        for msg in other_messages:
            self._handle_agent_message(msg)

        self._maybe_retry_requests()
        if self.config.get("enable_offense", True):
            self._maybe_attempt_offense()
        if self.config.get("enable_fuzzy_poisoning", False):
            self._maybe_poison_well()

    # ------------------------------------------------------------------
    # Moderator instructions (Strategy 1: Flawless Executor)
    # ------------------------------------------------------------------

    def _handle_moderator_message(self, message: Dict) -> None:
        body = message.get("body", "")

        round_match = ROUND_PATTERN.search(body)
        round_number = int(round_match.group(1)) if round_match else self.current_round

        if round_number != self._last_processed_round:
            self._reset_round_state()
            self._last_processed_round = round_number

        assigned_message = self._extract_assigned_message(body)
        if assigned_message is not None:
            self.my_assigned_message = assigned_message
            self.my_messages_by_round[round_number] = assigned_message

        request_list = self._extract_list(body, REQUEST_LIST_ANCHOR)
        if request_list is not None:
            self.request_list = request_list
            for agent_id in request_list:
                self.known_agents_this_game.add(agent_id)
                self._ensure_profile(agent_id)

        signing_list_raw = self._extract_list(body, SIGNING_LIST_ANCHOR)
        if signing_list_raw is not None:
            self._resolve_signing_list(signing_list_raw)

        # Immediate, deterministic action - no LLM deliberation.
        if self.my_assigned_message and self.request_list and not self.requested_from:
            body_text = self._build_request_body(round_number)
            for agent_id in self.request_list:
                self.send_message(
                    to_agent=agent_id,
                    subject=f"Signature Request - Round {round_number}",
                    body=body_text,
                )
                self.requested_from.add(agent_id)

    def _build_request_body(self, round_number: int) -> str:
        """Signature-request body. In round 2+ we quote our PREVIOUS round's
        assigned message so the recipient can resolve the fuzzy description that
        refers to us in their authorization list and actually sign for us.
        Without this hint, opponents that gate strictly on their resolved list
        decline (observed in the live logs as zero round-2/3 collection).
        Revealing our own past message is safe - it can never cause us a penalty
        and is information the recipient is meant to have anyway.
        """
        base = f"Please sign this message for me: {self.my_assigned_message}"
        if self.config.get("include_prev_message_hint", True):
            prev = self.my_messages_by_round.get(round_number - 1)
            if prev:
                base += f"\n\n(For reference, my message last round was: \"{prev}\")"
        return base

    def _maybe_retry_requests(self) -> None:
        """Safety net for the very first batch of a round: an instant,
        deterministic send can race the recipient's own round-start
        transition and get silently dropped (observed locally as round 1
        collection failing while rounds 2+ succeeded). Resend once, but only
        after letting a couple of batches pass so we're not just duplicating
        the original send before it had a chance to land.
        """
        if self.requests_retried_this_round or not self.my_assigned_message or not self.request_list:
            return
        if self.batches_seen_this_round < 2:
            return
        pending = [a for a in self.request_list if a not in self.signatures_received]
        if not pending:
            return
        self.requests_retried_this_round = True
        body_text = self._build_request_body(self._last_processed_round)
        for agent_id in pending:
            self.send_message(
                to_agent=agent_id,
                subject=f"Signature Request - Round {self._last_processed_round} (reminder)",
                body=body_text,
            )

    def _extract_assigned_message(self, body: str) -> Optional[str]:
        idx = body.find(ASSIGNED_MESSAGE_ANCHOR)
        if idx == -1:
            return None
        start = idx + len(ASSIGNED_MESSAGE_ANCHOR)
        end = body.find('"', start)
        if end == -1:
            return None
        return body[start:end]

    def _extract_list(self, body: str, anchor: str) -> Optional[List[str]]:
        for line in body.splitlines():
            idx = line.find(anchor)
            if idx != -1:
                remainder = line[idx + len(anchor):].strip()
                return _split_top_level_commas(remainder)
        return None

    # ------------------------------------------------------------------
    # Authorization resolution (Strategy 2: Impregnable Fortress)
    # ------------------------------------------------------------------

    def _resolve_signing_list(self, raw_entries: List[str]) -> None:
        for entry in raw_entries:
            if entry.endswith(FUZZY_SUFFIX):
                phrase = entry[: -len(FUZZY_SUFFIX)].strip()
                resolved = self._resolve_fuzzy_description(phrase)
                if resolved:
                    self.resolved_signing_list.add(resolved)
                else:
                    self.unresolved_fuzzy_raw.append(entry)
            else:
                self.resolved_signing_list.add(entry)
                self.known_agents_this_game.add(entry)
                self._ensure_profile(entry)

    def _resolve_fuzzy_description(self, phrase: str) -> Optional[str]:
        """Match a round 2+ fuzzy description against our own observed message
        history. The LLM only proposes a candidate; the code below only accepts
        an answer that exactly matches an agent we genuinely have history for -
        this is what stops a manipulated description (or any injected text
        elsewhere in an email body) from talking the model into an unlisted
        or hallucinated answer.
        """
        candidates = {a: m for a, m in self.message_history_by_agent.items() if m}
        if not candidates:
            return None

        candidate_block = "\n".join(
            f"- {agent_id}: " + " | ".join(msgs) for agent_id, msgs in candidates.items()
        )
        prompt = (
            "You are matching a paraphrased description to the agent it refers to, "
            "using ONLY the message history below. The description paraphrases something "
            "an agent literally said in an earlier round.\n\n"
            f'Description: "{phrase}"\n\n'
            f"Candidate agents and their past messages:\n{candidate_block}\n\n"
            "Respond with ONLY the exact agent id that matches, and nothing else. "
            "If you are not highly confident, respond with exactly: NONE"
        )
        answer = self._call_llm_text(prompt, temperature=0.0, max_tokens=20)
        if not answer:
            return None
        answer = answer.strip().strip('"').strip(".")
        return answer if answer in candidates else None

    def _call_llm_text(self, prompt: str, temperature: float = 0.0, max_tokens: int = 60) -> Optional[str]:
        try:
            client = self.driver._openai_client
            response = client.chat.completions.create(
                model=self.driver.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except Exception as exc:
            print(f"[{self.agent_id}] LLM call failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Other agents' emails
    # ------------------------------------------------------------------

    def _handle_agent_message(self, message: Dict) -> None:
        from_agent = message.get("from", "")
        body = message.get("body", "")
        if not from_agent or from_agent == self.agent_id:
            return

        self.known_agents_this_game.add(from_agent)
        self._ensure_profile(from_agent)
        self.message_history_by_agent.setdefault(from_agent, []).append(body)

        signed_message = self.extract_signed_message_from_email(body)
        if signed_message:
            # If the sender wasn't on our request_list, they weren't authorized
            # to sign for us - this is a successful offensive play. Record it
            # so future targeting can prioritize agents who actually fall for it.
            if from_agent not in self.request_list:
                self.opponent_profiles[from_agent]["times_they_signed_unauthorized_for_us"] += 1

            self.submit_signature(signed_message)
            self.signatures_received.add(from_agent)
            return

        message_to_sign = _extract_message_to_sign(body)
        if message_to_sign is None:
            return  # plain chatter - logged for fuzzy resolution, no reply

        if from_agent in self.resolved_signing_list:
            self.sign_and_respond(
                to_agent=from_agent,
                message_to_sign=message_to_sign,
                response_body="Here is your signed message.",
                subject="Signed Message",
            )
        else:
            self.opponent_profiles[from_agent]["unauthorized_requests_from_them"] += 1
            self.send_message(
                to_agent=from_agent,
                subject="Re: Signature Request",
                body="Sorry, I cannot verify that I am authorized to sign for you at this time.",
            )

    # ------------------------------------------------------------------
    # Offense (Strategy 3: Cautious Saboteur)
    # ------------------------------------------------------------------

    def _maybe_attempt_offense(self) -> None:
        if self.offense_attempted_this_round or not self.my_assigned_message:
            return

        # By construction (src/game/assignment.py), request_list is exactly the
        # set of agents actually authorized to sign for us. Anyone else who
        # signs for us anyway is, by definition, unauthorized - costing them
        # a point and costing us nothing to ask.
        blocklist = set(self.config.get("offense_blocklist", []))
        candidates = self.known_agents_this_game - set(self.request_list) - {self.agent_id} - blocklist

        # Adaptive Learner: drop agents who have proven to be reliable decliners
        # (asked give_up_after+ times across games, never once signed).
        give_up_after = self.config.get("offense_give_up_after", 2)

        def is_proven_decliner(agent_id: str) -> bool:
            p = self.opponent_profiles.get(agent_id, {})
            return (p.get("offense_attempts", 0) >= give_up_after
                    and p.get("times_they_signed_unauthorized_for_us", 0) == 0)

        candidates = {a for a in candidates if not is_proven_decliner(a)}
        if not candidates:
            return

        def gullibility(agent_id: str) -> int:
            return self.opponent_profiles.get(agent_id, {}).get("times_they_signed_unauthorized_for_us", 0)

        # Find all candidates tied for the highest gullibility score and pick
        # one at random, so we're not always targeting the same agent (e.g.
        # alphabetically) when scores are tied or all still zero.
        gullibility_scores = {agent_id: gullibility(agent_id) for agent_id in candidates}
        max_gullibility = max(gullibility_scores.values())
        top_targets = [agent for agent, score in gullibility_scores.items() if score == max_gullibility]
        target = random.choice(top_targets)

        self.offense_attempted_this_round = True  # set first so a failure doesn't retry every batch
        self.opponent_profiles[target]["offense_attempts"] += 1

        # Budget-free static template (no LLM call); rotate for mild unpredictability.
        body = random.choice(OFFENSE_TEMPLATES).format(target=target, message=self.my_assigned_message)
        self.send_message(to_agent=target, subject="Quick favor", body=body)

    # ------------------------------------------------------------------
    # Fuzzy-description poisoning (secondary, opportunistic, feature-flagged)
    # ------------------------------------------------------------------

    def _maybe_poison_well(self) -> None:
        if self._poisoned_this_game or self.current_round != 1:
            return

        candidates = [(a, m[0]) for a, m in self.message_history_by_agent.items() if m]
        if not candidates or len(self.known_agents_this_game) < 2:
            return

        # Pick a random opponent's message to echo, and a random recipient,
        # so this tactic isn't deterministically the same pairing every game.
        source_agent, source_message = random.choice(candidates)
        other_agents = [
            a for a in self.known_agents_this_game if a not in (source_agent, self.agent_id)
        ]
        if not other_agents:
            return
        target = random.choice(other_agents)

        prompt = (
            "Write one short, casual chat sentence (not a request) that loosely echoes the "
            "theme/imagery of the message below, as if reminiscing about something funny someone "
            "said earlier. Do not quote it verbatim and do not mention signatures or authorization.\n\n"
            f'Message to echo the theme of: "{source_message}"'
        )
        text = self._call_llm_text(prompt, temperature=0.9, max_tokens=60)
        if not text:
            return
        self.send_message(to_agent=target, subject="Random thought", body=text.strip())
        self._poisoned_this_game = True
