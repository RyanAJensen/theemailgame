"""Simple custom agent for The Email Game.

Design goals:
- Correctly process moderator instructions.
- Keep per-game state small and explicit.
- Handle clear signature workflows deterministically.
- Fall back to the built-in LLM behavior when instructions are ambiguous.
- Keep logs safe and non-sensitive.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.base_agent import BaseAgent


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

_ALLOWED_MODELS = {"gpt-4.1", "gpt-4.1-mini"}
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ROUND_RE = re.compile(r"\bROUND\s+(\d+)\b", re.IGNORECASE)
_SIGNED_MARKER = "SIGNED_MESSAGE_JSON:"


class CustomAgent(BaseAgent):
    """Custom agent with conservative rule handling and safe LLM fallback."""

    def __init__(self, *args, **kwargs):
        # Keep the default cheap so smoke tests do not burn budget.
        model = kwargs.get("model") or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        if model not in _ALLOWED_MODELS:
            logger.warning("Unsupported model requested: %s; forcing gpt-4.1-mini", model)
            model = "gpt-4.1-mini"
        kwargs["model"] = model
        super().__init__(*args, **kwargs)

        self.current_round = 0
        self.current_assigned_message: Optional[str] = None
        self.current_request_targets: List[str] = []
        self.current_authorized_signers: List[str] = []
        self.current_authorization_explicit = False

    def on_new_game(self) -> None:
        self.current_round = 0
        self.current_assigned_message = None
        self.current_request_targets = []
        self.current_authorized_signers = []
        self.current_authorization_explicit = False
        logger.info("Reset per-game state")

    def on_message_batch(self, messages: List[Dict]) -> None:
        handled_indexes = set()

        for index, message in enumerate(messages):
            sender = str(message.get("from", "")).strip().lower()

            if sender == "moderator":
                handled_indexes.add(index)
                self._handle_moderator_message(message)
                continue

            if self._maybe_submit_received_signature(message):
                handled_indexes.add(index)
                continue

            if self._maybe_handle_signature_request(message):
                handled_indexes.add(index)
                continue

        fallback = [m for i, m in enumerate(messages) if i not in handled_indexes]
        if fallback:
            super().on_message_batch(fallback)

    def _handle_moderator_message(self, message: Dict) -> None:
        body = message.get("body", "") or ""
        subject = message.get("subject", "") or ""

        parsed_round = self._extract_round_number(body, subject)
        if parsed_round is not None:
            self.current_round = parsed_round
        elif self.current_round == 0:
            # Last-resort fallback if the moderator text is malformed.
            self.current_round = 1
        else:
            logger.warning("Moderator round number was missing; keeping round %s", self.current_round)

        self.current_assigned_message = None
        self.current_request_targets = []
        self.current_authorized_signers = []
        self.current_authorization_explicit = False

        assigned = self._extract_value_after_prefix(
            body,
            [
                "EXACT message",
                "Your message this round is",
                "You must get signatures for this EXACT message",
            ],
        )
        if assigned:
            self.current_assigned_message = assigned

        request_targets, request_targets_explicit = self._extract_agent_list_after_prefix(
            body,
            [
                "you must REQUEST signatures from these agents",
                "request signatures from these agents",
                "request signatures from these",
            ],
        )
        if request_targets_explicit:
            self.current_request_targets = self._normalize_agent_list(request_targets)

        authorized_signers, authorized_explicit = self._extract_agent_list_after_prefix(
            body,
            [
                "you are authorized to sign for",
                "you may sign for",
                "you are allowed to sign for",
                "you are authorized to sign these agents",
            ],
        )
        if authorized_signers:
            self.current_authorized_signers = self._normalize_agent_list(authorized_signers)
            self.current_authorization_explicit = authorized_explicit

        logger.info(
            "Round %s: assigned=%s request_targets=%s auth_mode=%s subject=%s",
            self.current_round,
            bool(self.current_assigned_message),
            len(self.current_request_targets),
            "explicit" if self.current_authorization_explicit else "ambiguous",
            subject,
        )

        if self.current_assigned_message and self.current_request_targets:
            for agent_id in self._dedupe(self.current_request_targets):
                # TODO(strategy): add resend/dedupe state if the network drops an outbound request.
                self.send_message(
                    to_agent=agent_id,
                    subject=f"Signature Request - Round {self.current_round}",
                    body=f"Hi {agent_id}, please sign this message for me: {self.current_assigned_message}",
                )
                logger.info("Sent signature request to %s", agent_id)
            return

        # If the moderator text is not fully parseable, let the shipped LLM logic handle it.
        super().on_message_batch([message])

    def _maybe_submit_received_signature(self, message: Dict) -> bool:
        body = message.get("body", "") or ""
        if _SIGNED_MARKER not in body:
            return False

        payload = body.split(_SIGNED_MARKER, 1)[1].strip()
        if not payload:
            return False

        match = re.search(r"\{.*\}", payload, re.DOTALL)
        if not match:
            return False

        try:
            signed_message = json.loads(match.group(0))
        except json.JSONDecodeError:
            return False

        self.submit_signature(signed_message)
        logger.info("Submitted received signature")
        return True

    def _maybe_handle_signature_request(self, message: Dict) -> bool:
        body = message.get("body", "") or ""
        request = self._extract_requested_message(body)
        if not request:
            return False

        requester = self._normalize_agent_id(str(message.get("from", "")))
        if self.current_authorization_explicit and requester in self.current_authorized_signers:
            self.sign_and_respond(
                to_agent=requester,
                message_to_sign=request,
                response_body="Signed as requested.",
                subject="Signed Message",
            )
            logger.info("Signed request from %s", requester)
            return True

        if self.current_authorization_explicit:
            self.send_message(
                to_agent=requester,
                subject="Cannot sign this request",
                body=(
                    "I cannot sign that request right now. "
                    "(I only sign for explicitly authorized agents in this round.)"
                ),
            )
            logger.info("Declined unauthorized request from %s", requester)
            return True

        # Ambiguous authorization context: let the shipped LLM logic inspect the request.
        return False

    @staticmethod
    def _extract_round_number(body: str, subject: str) -> Optional[int]:
        for text in (subject, body):
            match = _ROUND_RE.search(text or "")
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _extract_value_after_prefix(body: str, prefixes: Sequence[str]) -> Optional[str]:
        for line in body.splitlines():
            raw = line.strip()
            lower = raw.lower()
            for prefix in prefixes:
                if prefix.lower() in lower and ":" in raw:
                    value = raw.split(":", 1)[1].strip()
                    return value.strip().strip('"').strip("'")
        return None

    @staticmethod
    def _extract_agent_list_after_prefix(body: str, prefixes: Sequence[str]) -> Tuple[List[str], bool]:
        for line in body.splitlines():
            raw = line.strip()
            lower = raw.lower()
            for prefix in prefixes:
                if prefix.lower() in lower and ":" in raw:
                    tail = raw.split(":", 1)[1].strip()
                    items = [item.strip().strip('"').strip("'") for item in tail.split(",")]
                    items = [item for item in items if item]
                    if items:
                        explicit = all(CustomAgent._looks_like_agent_id(item) for item in items)
                        return items, explicit
        return [], False

    @staticmethod
    def _extract_requested_message(body: str) -> Optional[str]:
        patterns = [
            r"please\s+sign\s+this\s+message\s+for\s+me\s*:?\s*(?:\"?)(.+?)(?:\"?$)",
            r"sign\s+this\s+message\s*:?\s*(?:\"?)(.+?)(?:\"?$)",
            r"message\s+to\s+sign\s*:?\s*(?:\"?)(.+?)(?:\"?$)",
        ]
        for line in body.splitlines():
            for pattern in patterns:
                match = re.search(pattern, line, flags=re.IGNORECASE)
                if match:
                    return match.group(1).strip().strip('"').strip("'")
        return None

    @staticmethod
    def _looks_like_agent_id(value: str) -> bool:
        return bool(_AGENT_ID_RE.match(value.strip()))

    @staticmethod
    def _normalize_agent_id(value: str) -> str:
        return value.strip().lower()

    @classmethod
    def _normalize_agent_list(cls, values: Sequence[str]) -> List[str]:
        normalized: List[str] = []
        for value in values:
            item = cls._normalize_agent_id(value)
            if item:
                normalized.append(item)
        return normalized

    @staticmethod
    def _dedupe(values: Sequence[str]) -> List[str]:
        seen = set()
        ordered: List[str] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                ordered.append(value)
        return ordered
