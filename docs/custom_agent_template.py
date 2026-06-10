"""
The Email Game – Custom Agent Template

Copy this file, rename it, and implement your strategy below.
Run it with:

    python scripts/run_custom_agent.py my_agent "My Agent" --module my_agent.py

Your class must be named CustomAgent and must subclass BaseAgent.

The runner also accepts (all optional, they stack with your code):
    --prompt my_prompt.md   system prompt for any LLM calls (default: docs/agent_prompt.md)
    --model gpt-4.1         which OpenAI model to use         (default: gpt-4.1)
    --temperature 0.7       LLM randomness 0.0-2.0            (default: 1.0)
See the "Customizing Agents" section of the README for the full rundown.

IMPORTANT - the files in data/ are SAMPLES for local testing only. The live
competition server uses different, PRIVATE data. In particular,
data/message_alias_pool.json (the round-2+ fuzzy descriptions) and
data/sample_agents.json will NOT match the real game, so reading/hardcoding them
will fail live. Resolve fuzzy descriptions by reasoning from the message history
you actually receive each round, not by looking them up in a shipped file.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.base_agent import BaseAgent


class CustomAgent(BaseAgent):
    """
    Your custom The Email Game agent.

    The key method to override is on_message_batch(messages). It is called
    every time a new batch of emails arrives. By default it forwards them to
    an LLM. Override it to replace or augment that behavior with your own logic.

    Available action methods (inherited from BaseAgent):
        self.send_message(to_agent, subject, body)
        self.sign_message(message, for_agent)         -> signed_message dict
        self.sign_and_respond(to_agent, message_to_sign, response_body, subject)
        self.submit_signature(signed_message)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Add any state you want to track across rounds here.
        self.round_number = 0

    def on_new_game(self) -> None:
        """Called at the start of each new game (round 1).

        In the live ladder your agent is reused for many back-to-back games.
        The built-in LLM context is reset for you automatically; reset any state
        YOU track here so it does not leak between games. If you skip this, your
        own memory accumulates every game (growing cost if you feed it to an LLM,
        and stale data from previous games).
        """
        self.round_number = 0

    def on_message_batch(self, messages: List[Dict]) -> None:
        """
        Called with each batch of fresh incoming emails.

        Each message dict has these keys:
            from        — sender agent ID (or "moderator")
            to          — your agent ID
            subject     — email subject
            body        — email body text
            message_id  — unique ID
            timestamp   — ISO-8601 string

        The default implementation forwards to the LLM. Replace or extend it
        with your own logic. Call super().on_message_batch(messages) to keep
        LLM behavior for messages you don't handle yourself.

        Example: handle moderator messages manually, fall back to LLM for the rest.
        """
        moderator_messages = [m for m in messages if m.get("from") == "moderator"]
        other_messages = [m for m in messages if m.get("from") != "moderator"]

        for msg in moderator_messages:
            self._handle_moderator_message(msg)

        if other_messages:
            # Let the LLM handle everything else
            super().on_message_batch(other_messages)

    def _handle_moderator_message(self, message: Dict) -> None:
        """
        Example: parse a moderator message and act on it directly without the LLM.
        Remove this and call super().on_message_batch([message]) to use LLM instead.
        """
        self.round_number += 1
        body = message.get("body", "")

        # Parse your assigned message. The moderator format (see the round
        # instructions you actually receive) is:
        #   You must get signatures for this EXACT message: "<message>"
        assigned_message = None
        for line in body.splitlines():
            if "EXACT message:" in line:
                assigned_message = line.split("EXACT message:", 1)[1].strip().strip('"')
                break

        if not assigned_message:
            # Couldn't parse — let the built-in LLM handle this message instead.
            super().on_message_batch([message])
            return

        print(f"[{self.agent_id}] Round {self.round_number} — assigned: {assigned_message}")

        # Parse your request list. The moderator format is a single line:
        #   1. You must REQUEST signatures from these agents: alice, bob
        # (comma-separated agent ids after the colon).
        request_list = []
        for line in body.splitlines():
            if "request signatures from these agents:" in line.lower():
                names = line.split(":", 1)[1]
                request_list = [n.strip() for n in names.split(",") if n.strip()]
                break

        # Send signature requests for your assigned message.
        for agent_id in request_list:
            self.send_message(
                to_agent=agent_id,
                subject=f"Signature Request - Round {self.round_number}",
                body=f"Hi {agent_id}, please sign this message for me: {assigned_message}",
            )
