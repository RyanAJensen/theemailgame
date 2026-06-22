"""Custom agent scaffold for The Email Game.

Design goals:
- Parse moderator instructions deterministically first.
- Handle signature requests and returned signatures safely.
- Keep state small, explicit, and resettable.
- Fall back to the repo's BaseAgent / LLM path only when needed.
- Avoid secrets, unsupported models, and long-running loops.
"""

import json
import logging
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

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
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "i", "in", "is", "it", "its", "me", "my", "of", "on", "or", "our", "the",
    "their", "this", "that", "to", "was", "were", "with", "you", "your",
}

_ASSIGNED_PATTERNS = (
    r"(?im)^\s*(?:[-*\d.)\s]*)?(?:your|my|the)?\s*(?:assigned\s+)?message(?:\s+this\s+round)?\s*(?:is|:|-|—)\s*(.+)$",
    r"(?im)^\s*(?:[-*\d.)\s]*)?(?:exact\s+message|assigned\s+message|message\s+to\s+sign|message\s+you\s+must\s+sign|message\s+assigned\s+to\s+you)\s*(?:is|:|-|—)\s*(.+)$",
    r"(?im)^\s*(?:[-*\d.)\s]*)?(?:you\s+must\s+get\s+signatures\s+for\s+this\s+exact\s+message|you\s+must\s+request\s+signatures\s+for\s+this\s+exact\s+message)\s*(?:is|:|-|—)\s*(.+)$",
)
_REQUEST_PATTERNS = (
    r"(?im)^\s*(?:[-*\d.)\s]*)?(?:you\s+must\s+)?request(?:\s+signatures)?\s+from(?:\s+these)?(?:\s+agents)?\s*(?:is|are|:|-|—)\s*(.+)$",
    r"(?im)^\s*(?:[-*\d.)\s]*)?(?:request\s+signatures\s+from\s+these\s+agents|request\s+signatures\s+from\s+these|request\s+list|request\s+targets|your\s+request\s+list)\s*(?:is|are|:|-|—)\s*(.+)$",
)
_AUTH_PATTERNS = (
    r"(?im)^\s*(?:[-*\d.)\s]*)?(?:you\s+are\s+authorized\s+to\s+sign(?:\s+messages)?\s+for(?:\s+these)?(?:\s+agents)?|you\s+may\s+sign(?:\s+messages)?\s+for(?:\s+these)?(?:\s+agents)?|you\s+are\s+allowed\s+to\s+sign(?:\s+messages)?\s+for(?:\s+these)?(?:\s+agents)?|authorized\s+to\s+sign(?:\s+messages)?\s+for(?:\s+these)?(?:\s+agents)?|signing\s+permissions(?:\s+for(?:\s+these)?(?:\s+agents)?)?|sign\s+permissions(?:\s+for(?:\s+these)?(?:\s+agents)?)?)\s*(?:is|are|:|-|—)\s*(.+)$",
)


class CustomAgent(BaseAgent):
    """Conservative but competitive Email Game agent."""

    def __init__(self, *args, **kwargs):
        model = kwargs.get("model") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
        if model not in _ALLOWED_MODELS:
            logger.warning("Unsupported model requested: %s; forcing gpt-4.1-mini", model)
            model = "gpt-4.1-mini"
        kwargs["model"] = model
        super().__init__(*args, **kwargs)

        self._reset_game_state()

    def _reset_game_state(self) -> None:
        self.current_round = 0
        self.current_assigned_message: Optional[str] = None
        self.current_request_targets: List[str] = []
        self.current_authorized_signers: List[str] = []
        self.current_authorized_descriptions: List[str] = []
        self.current_authorization_resolved: List[str] = []
        self.current_authorization_has_fuzzy = False
        self._seen_message_ids: Set[str] = set()
        self._sent_request_keys: Set[Tuple[int, str, Tuple[str, ...]]] = set()
        self._submitted_signature_keys: Set[Tuple[str, str, str]] = set()
        self._handled_request_keys: Set[Tuple[str, str]] = set()
        self._declined_request_keys: Set[Tuple[str, str]] = set()
        self._processed_moderator_keys: Set[Tuple[int, str, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]] = set()
        self._pending_signature_requests: Dict[str, Set[str]] = {}
        self._sender_history: Dict[str, List[str]] = {}

    def on_new_game(self) -> None:
        self._reset_game_state()
        logger.info("Reset per-game state")

    def on_message_batch(self, messages: List[Dict]) -> None:
        fallback: List[Dict] = []
        non_moderator_messages: List[Dict] = []

        for message in messages:
            message_id = str(message.get("message_id") or "").strip()
            if message_id and message_id in self._seen_message_ids:
                continue
            if message_id:
                self._seen_message_ids.add(message_id)

            sender = self._normalize_agent_id(str(message.get("from", "")))
            if sender and sender != "moderator":
                self._remember_sender_message(sender, str(message.get("body", "") or ""))

            if sender == "moderator":
                if self._handle_moderator_message(message):
                    continue
                fallback.append(message)
                continue

            non_moderator_messages.append(message)

        # Process moderator instructions first so request and signature handling
        # in the same batch sees the current round state instead of stale data.
        for message in non_moderator_messages:
            if self._maybe_submit_received_signature(message):
                continue

            if self._maybe_handle_signature_request(message):
                continue

            fallback.append(message)

        if fallback:
            super().on_message_batch(fallback)

    def _handle_moderator_message(self, message: Dict) -> bool:
        body = str(message.get("body", "") or "")
        subject = str(message.get("subject", "") or "")

        parsed_round = self._extract_round_number(body, subject)
        if parsed_round is None:
            if self.current_round == 0:
                self.current_round = 1
            else:
                logger.warning("Moderator round number missing; keeping round %s", self.current_round)
        else:
            if self.current_round and parsed_round < self.current_round:
                logger.info(
                    "Ignoring stale moderator message for round %s (current round %s)",
                    parsed_round,
                    self.current_round,
                )
                return True
            self.current_round = parsed_round

        assigned = self._extract_first_labeled_value(body, _ASSIGNED_PATTERNS)
        request_targets = self._extract_agent_list(body, _REQUEST_PATTERNS, require_explicit=True)
        auth_ids, auth_descriptions = self._extract_authorization_entries(body)

        if not assigned or not request_targets:
            logger.info(
                "Round %s moderator message not fully parsed; falling back to BaseAgent",
                self.current_round,
            )
            return False

        self.current_assigned_message = assigned
        self.current_request_targets = self._dedupe(request_targets)
        self.current_authorized_signers = auth_ids
        self.current_authorized_descriptions = auth_descriptions
        self.current_authorization_has_fuzzy = bool(auth_descriptions)
        self._refresh_authorization_resolution()

        moderator_key = (
            self.current_round,
            self.current_assigned_message,
            tuple(sorted(self.current_request_targets)),
            tuple(self.current_authorized_signers),
            tuple(self.current_authorized_descriptions),
        )
        if moderator_key in self._processed_moderator_keys:
            logger.info("Round %s moderator batch already handled; skipping duplicate message", self.current_round)
            return True
        self._processed_moderator_keys.add(moderator_key)

        logger.info(
            "Round %s: assigned=%s request_targets=%s auth_ids=%s fuzzy=%s",
            self.current_round,
            True,
            len(self.current_request_targets),
            len(self.current_authorized_signers),
            len(self.current_authorized_descriptions),
        )

        request_key = (self.current_round, self.current_assigned_message, tuple(sorted(self.current_request_targets)))
        if request_key in self._sent_request_keys:
            logger.info("Round %s request batch already sent; skipping duplicate moderator batch", self.current_round)
            return True
        self._sent_request_keys.add(request_key)

        pending = self._pending_signature_requests.setdefault(self.current_assigned_message, set())
        pending.update(self.current_request_targets)

        for agent_id in self._dedupe(self.current_request_targets):
            # TODO(strategy): learn which request targets are fastest / most reliable in live ladders.
            self.send_message(
                to_agent=agent_id,
                subject="Request for Signature",
                body=f"Please sign this message for me: {self.current_assigned_message}",
            )
            logger.info("Sent signature request to %s", agent_id)

        return True

    def _maybe_submit_received_signature(self, message: Dict) -> bool:
        body = str(message.get("body", "") or "")
        if _SIGNED_MARKER not in body:
            return False

        signed_message = self._extract_signed_message(body)
        if not signed_message:
            return True

        original_message = str(signed_message.get("original_message", ""))
        signer = self._normalize_agent_id(str(signed_message.get("signer", "")))
        signed_for = self._normalize_agent_id(str(signed_message.get("signed_for", "")))
        pending_signers = self._pending_signature_requests.get(original_message)
        if not pending_signers:
            logger.info(
                "Ignoring signed payload for untracked message: signer=%s signed_for=%s original=%r current=%r",
                signer or "<unknown>",
                signed_for or "<unknown>",
                original_message,
                self.current_assigned_message,
            )
            return True
        if signer not in pending_signers:
            logger.warning(
                "Ignoring signature from unexpected signer %s for message %r",
                signer or "<unknown>",
                original_message,
            )
            return True
        if signed_for and signed_for != self.agent_id:
            logger.warning("Ignoring signature for unexpected recipient %s", signed_for)
            return True

        key = (
            signer,
            signed_for,
            original_message,
        )
        if key in self._submitted_signature_keys:
            logger.info("Duplicate signed payload ignored: %s -> %s", key[0], key[1])
            return True

        logger.info(
            "Submitting signed payload from %s for %s (message=%r)",
            key[0] or "<unknown>",
            key[1] or "<unknown>",
            original_message,
        )
        result = self.submit_signature(signed_message)
        if result.get("success"):
            self._submitted_signature_keys.add(key)
            pending_signers.discard(signer)
            if not pending_signers:
                self._pending_signature_requests.pop(original_message, None)
            logger.info("Submitted received signature from %s", key[0])
        else:
            logger.warning("Signature submission failed for %s -> %s", key[0], key[1])
        return True

    def _maybe_handle_signature_request(self, message: Dict) -> bool:
        body = str(message.get("body", "") or "")
        request = self._extract_requested_message(body)
        if not request:
            return False

        requester = self._normalize_agent_id(str(message.get("from", "")))
        if not requester:
            return False

        self._refresh_authorization_resolution()
        request_key = (requester, request)
        if request_key in self._handled_request_keys:
            logger.info("Duplicate signature request ignored from %s", requester)
            return True

        if requester in self.current_authorization_resolved:
            result = self.sign_and_respond(
                to_agent=requester,
                message_to_sign=request,
                response_body="Signed as requested.",
                subject="Signed Message",
            )
            if result.get("success"):
                self._handled_request_keys.add(request_key)
                logger.info("Signed request from %s", requester)
            else:
                logger.warning("Failed to sign request from %s", requester)
            return True

        if request_key in self._declined_request_keys:
            return True

        self.send_message(
            to_agent=requester,
            subject="Cannot sign this request",
            body=(
                "I cannot sign that request. Please send only to agents I am "
                "authorized to sign for this round."
            ),
        )
        self._declined_request_keys.add(request_key)
        logger.info("Declined request from %s", requester)
        return True

    def _refresh_authorization_resolution(self) -> None:
        resolved: List[str] = []
        explicit = self._normalize_agent_list(self.current_authorized_signers)
        for agent_id in explicit:
            if agent_id and agent_id not in resolved:
                resolved.append(agent_id)

        unresolved: List[str] = []
        for description in self.current_authorized_descriptions:
            candidate = self._resolve_authorized_description(description)
            if candidate and candidate not in resolved:
                resolved.append(candidate)
            elif description:
                unresolved.append(description)

        self.current_authorization_resolved = resolved
        self.current_authorized_descriptions = unresolved

    def _resolve_authorized_description(self, description: str) -> Optional[str]:
        desc = self._clean_text(description)
        desc_tokens = self._content_tokens(desc)
        if not desc_tokens:
            return None

        best_sender: Optional[str] = None
        best_score = 0.0
        second_best = 0.0

        for sender, bodies in self._sender_history.items():
            sender_text = " ".join(bodies[-8:])
            sender_tokens = self._content_tokens(sender_text)
            overlap = len(desc_tokens & sender_tokens)
            similarity = 0.0
            for body in bodies[-5:]:
                similarity = max(similarity, SequenceMatcher(None, desc, self._clean_text(body)).ratio())
            score = overlap + similarity
            if score > best_score:
                second_best = best_score
                best_score = score
                best_sender = sender
            elif score > second_best:
                second_best = score

        if best_sender and best_score >= 2.0 and best_score >= second_best + 0.25:
            return best_sender
        return None

    def _remember_sender_message(self, sender: str, body: str) -> None:
        if not sender or not body:
            return
        history = self._sender_history.setdefault(sender, [])
        history.append(body)
        if len(history) > 24:
            del history[:-24]

    @staticmethod
    def _extract_round_number(body: str, subject: str) -> Optional[int]:
        for text in (subject, body):
            match = _ROUND_RE.search(text or "")
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _extract_first_labeled_value(body: str, patterns: Sequence[str]) -> Optional[str]:
        for pattern in patterns:
            match = re.search(pattern, body)
            if match:
                value = match.group(1).strip()
                value = CustomAgent._strip_wrapping_quotes(value)
                if value and re.search(r"[A-Za-z0-9]", value):
                    return value
        return None

    @staticmethod
    def _extract_agent_list(body: str, patterns: Sequence[str], require_explicit: bool) -> List[str]:
        for pattern in patterns:
            match = re.search(pattern, body)
            if not match:
                continue

            tail = CustomAgent._strip_wrapping_quotes(match.group(1).strip())
            parts = [part.strip() for part in re.split(r"[,\n;]+", tail) if part.strip()]
            if not parts:
                continue

            if require_explicit:
                explicit = [part for part in parts if CustomAgent._looks_like_agent_id(part)]
                if len(explicit) != len(parts):
                    return []
                return CustomAgent._normalize_agent_list(explicit)

            return [part for part in parts if part]
        return []

    @staticmethod
    def _extract_authorization_entries(body: str) -> Tuple[List[str], List[str]]:
        for pattern in _AUTH_PATTERNS:
            match = re.search(pattern, body)
            if not match:
                continue

            tail = CustomAgent._strip_wrapping_quotes(match.group(1).strip())
            parts = [part.strip() for part in re.split(r"[,\n;]+", tail) if part.strip()]
            if not parts:
                continue

            explicit = [part for part in parts if CustomAgent._looks_like_agent_id(part)]
            descriptions = [part for part in parts if part not in explicit]
            return CustomAgent._normalize_agent_list(explicit), descriptions

        return [], []

    @staticmethod
    def _extract_requested_message(body: str) -> Optional[str]:
        patterns = (
            r"(?im)^\s*(?:[-*\d.)\s]*)?(?:please\s+)?sign\s+this\s+message\s+for\s+me\s*[:\-]\s*(.+)$",
            r"(?im)^\s*(?:[-*\d.)\s]*)?(?:please\s+)?sign\s+the\s+following\s+message\s*[:\-]\s*(.+)$",
            r"(?im)^\s*(?:[-*\d.)\s]*)?message\s+to\s+sign\s*[:\-]\s*(.+)$",
            r"(?im)^\s*(?:[-*\d.)\s]*)?(?:sign\s+this\s+message\s*[:\-]\s*(.+)$)",
        )
        for pattern in patterns:
            match = re.search(pattern, body)
            if match:
                value = CustomAgent._strip_wrapping_quotes(match.group(1).strip())
                if value:
                    return value
        return None

    @staticmethod
    def _extract_signed_message(body: str) -> Optional[Dict]:
        text = body.strip()
        if not text:
            return None

        required_keys = {"original_message", "signature", "signer", "signed_for"}
        decoder = json.JSONDecoder()

        def _extract_from_fragment(fragment: str) -> Optional[Dict]:
            start = fragment.find("{")
            if start < 0:
                return None
            try:
                signed_message, _ = decoder.raw_decode(fragment[start:])
            except json.JSONDecodeError:
                return None
            if not isinstance(signed_message, dict):
                return None
            if required_keys.issubset(signed_message.keys()):
                return signed_message
            for nested_key in ("signed_message", "payload", "message", "signature"):
                nested = signed_message.get(nested_key)
                if isinstance(nested, dict) and required_keys.issubset(nested.keys()):
                    return nested
            return None

        marker_pattern = re.compile(r"SIGNED_MESSAGE_JSON\s*[:=]\s*", re.IGNORECASE)
        search_starts = [m.end() for m in marker_pattern.finditer(text)]
        search_starts.append(0)

        seen = set()
        for start in search_starts:
            if start in seen:
                continue
            seen.add(start)
            extracted = _extract_from_fragment(text[start:])
            if extracted is not None:
                return extracted

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

    @staticmethod
    def _strip_wrapping_quotes(value: str) -> str:
        text = value.strip()
        while len(text) >= 2 and ((text[0] == text[-1] == '"') or (text[0] == text[-1] == "'") or (text[0] == text[-1] == "`")):
            text = text[1:-1].strip()
        return text

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lower()

    @classmethod
    def _content_tokens(cls, value: str) -> Set[str]:
        tokens = set(re.findall(r"[a-z0-9']+", value.lower()))
        return {token for token in tokens if token not in _STOPWORDS and len(token) > 2}
