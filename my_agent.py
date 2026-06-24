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
    r"(?im)^\s*(?:[-*\d.)\s]*)?(?:you\s+must\s+get\s+signatures\s+for\s+this\s+exact\s+message)\s*(?:is|:|-|—)\s*(.+)$",
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
        self._handled_request_keys: Set[Tuple[int, str, str]] = set()
        self._declined_request_keys: Set[Tuple[int, str, str]] = set()
        self._handled_requesters_by_round: Dict[int, Set[str]] = {}
        self._declined_requesters_by_round: Dict[int, Set[str]] = {}
        self._processed_moderator_keys: Set[Tuple[int, str, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]] = set()
        self._pending_signature_requests: Dict[str, Set[str]] = {}
        self._pending_signature_rounds: Dict[str, int] = {}
        self._sender_history: Dict[str, List[str]] = {}
        self._mailbox_signature_scan_ids: Set[str] = set()

    def on_new_game(self) -> None:
        self._reset_game_state()
        logger.info("Reset per-game state")

    def on_message_batch(self, messages: List[Dict]) -> None:
        fallback: List[Dict] = []
        non_moderator_messages: List[Dict] = []

        for message in messages:
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

            if self._maybe_ignore_handled_signature_thread_reply(message):
                continue

            fallback.append(message)

        self._scan_mailbox_for_signed_messages()

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
                    "Skipped because stale: moderator message for round %s (current round %s)",
                    parsed_round,
                    self.current_round,
                )
                return True
            self.current_round = parsed_round
            self._drop_stale_pending_signature_requests()

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
        self._pending_signature_rounds[self.current_assigned_message] = self.current_round

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
        signed_message = self._extract_signed_message(body)
        if not signed_message:
            if "signed" in str(message.get("subject", "") or "").lower():
                logger.info("Signed subject seen but payload could not be parsed; falling back to BaseAgent")
                self._log_signed_payload_decision(
                    round_id=self.current_round,
                    signer="<unknown>",
                    assignment_matched=False,
                    submit_attempted=False,
                    submit_succeeded=False,
                    skipped_reason="parse failed",
                )
            return False

        original_message = str(signed_message.get("original_message", ""))
        signer = self._normalize_agent_id(str(signed_message.get("signer", "")))
        signed_for = self._normalize_agent_id(str(signed_message.get("signed_for", "")))
        pending_signers = self._pending_signature_requests.get(original_message)
        pending_round = self._pending_signature_rounds.get(original_message, self.current_round)
        assignment_matched = bool(
            original_message
            and original_message == self.current_assigned_message
            and pending_round == self.current_round
        )
        logger.info(
            "Received signed payload: signer=%s signed_for=%s original=%r current=%r pending=%s",
            signer or "<unknown>",
            signed_for or "<unknown>",
            original_message,
            self.current_assigned_message,
            sorted(pending_signers) if pending_signers else [],
        )
        key = (
            signer,
            signed_for,
            original_message,
        )
        if key in self._submitted_signature_keys:
            logger.info("Duplicate signed payload ignored: %s -> %s", key[0], key[1])
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=False,
                submit_succeeded=False,
                skipped_reason="duplicate",
            )
            return True
        if not original_message or not signer:
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=False,
                submit_succeeded=False,
                skipped_reason="parse failed",
            )
            return True
        if signed_for and signed_for != self.agent_id:
            logger.warning("Skipped because unauthorized: signature was for unexpected recipient %s", signed_for)
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=False,
                submit_succeeded=False,
                skipped_reason="unauthorized",
            )
            return True
        if pending_round != self.current_round:
            logger.info(
                "Skipped because stale: signed payload for round %s while current round is %s signer=%s signed_for=%s original=%r",
                pending_round,
                self.current_round,
                signer or "<unknown>",
                signed_for or "<unknown>",
                original_message,
            )
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=False,
                submit_succeeded=False,
                skipped_reason="stale",
            )
            return True
        if not pending_signers:
            logger.info(
                "Skipped because stale: signed payload for untracked message signer=%s signed_for=%s original=%r current=%r",
                signer or "<unknown>",
                signed_for or "<unknown>",
                original_message,
                self.current_assigned_message,
            )
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=False,
                submit_succeeded=False,
                skipped_reason="stale",
            )
            return True
        if signer not in pending_signers:
            logger.warning(
                "Skipped because missing required signer: got %s for message %r, pending=%s",
                signer or "<unknown>",
                original_message,
                sorted(pending_signers),
            )
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=False,
                submit_succeeded=False,
                skipped_reason=f"missing required signer: {signer or '<unknown>'}",
            )
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
                self._pending_signature_rounds.pop(original_message, None)
            logger.info("Submitted received signature from %s", key[0])
            logger.info("submitted signature for round %s from %s", pending_round, key[0])
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=True,
                submit_succeeded=True,
                skipped_reason="none",
            )
        else:
            logger.warning("Signature submission failed for %s -> %s", key[0], key[1])
            self._log_signed_payload_decision(
                round_id=pending_round,
                signer=signer or "<unknown>",
                assignment_matched=assignment_matched,
                submit_attempted=True,
                submit_succeeded=False,
                skipped_reason="submission failed",
            )
        return True

    def _drop_stale_pending_signature_requests(self) -> None:
        stale_messages = [
            message
            for message, round_id in self._pending_signature_rounds.items()
            if round_id < self.current_round
        ]
        for message in stale_messages:
            round_id = self._pending_signature_rounds.pop(message, None)
            self._pending_signature_requests.pop(message, None)
            logger.info(
                "Skipped because stale: clearing pending signatures for round %s after entering round %s",
                round_id,
                self.current_round,
            )

    @staticmethod
    def _log_signed_payload_decision(
        *,
        round_id: int,
        signer: str,
        assignment_matched: bool,
        submit_attempted: bool,
        submit_succeeded: bool,
        skipped_reason: str,
    ) -> None:
        logger.info(
            "Signed payload decision: current_round=%s signer=%s assignment_matched=%s submit_attempted=%s submit_succeeded=%s skipped_reason=%s",
            round_id,
            signer,
            "yes" if assignment_matched else "no",
            "yes" if submit_attempted else "no",
            "yes" if submit_succeeded else "no",
            skipped_reason,
        )

    def _maybe_handle_signature_request(self, message: Dict) -> bool:
        body = str(message.get("body", "") or "")
        request = self._extract_requested_message(body)
        if not request:
            return False

        requester = self._normalize_agent_id(str(message.get("from", "")))
        if not requester:
            return False

        self._refresh_authorization_resolution()
        request_key = (self.current_round, requester, request)
        if request_key in self._handled_request_keys:
            self._handled_requesters_by_round.setdefault(self.current_round, set()).add(requester)
            logger.info("Skipped because duplicate: signature request already handled from %s in round %s", requester, self.current_round)
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
                self._handled_requesters_by_round.setdefault(self.current_round, set()).add(requester)
                logger.info("Signed request from %s", requester)
            else:
                logger.warning("Failed to sign request from %s", requester)
            return True

        if request_key in self._declined_request_keys:
            self._declined_requesters_by_round.setdefault(self.current_round, set()).add(requester)
            logger.info("Skipped because duplicate: unauthorized request already declined from %s in round %s", requester, self.current_round)
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
        self._declined_requesters_by_round.setdefault(self.current_round, set()).add(requester)
        logger.info("Skipped because unauthorized: declined request from %s in round %s", requester, self.current_round)
        if not self.current_authorization_resolved:
            logger.info("Skipped because missing authorization: no resolved authorized signers for round %s", self.current_round)
        return True

    def _maybe_ignore_handled_signature_thread_reply(self, message: Dict) -> bool:
        sender = self._normalize_agent_id(str(message.get("from", "")))
        if not sender:
            return False

        subject = str(message.get("subject", "") or "")
        body = str(message.get("body", "") or "")
        if not self._looks_like_signature_thread_reply(subject, body):
            return False

        if sender in self._handled_requesters_by_round.get(self.current_round, set()):
            logger.info(
                "Skipped because duplicate: ignored signature-thread follow-up from already signed requester %s in round %s",
                sender,
                self.current_round,
            )
            return True

        if sender in self._declined_requesters_by_round.get(self.current_round, set()):
            logger.info(
                "Skipped because duplicate: ignored signature-thread follow-up from already declined requester %s in round %s",
                sender,
                self.current_round,
            )
            return True

        return False

    def _scan_mailbox_for_signed_messages(self) -> None:
        token = getattr(self, "_jwt_token", None)
        if not token:
            return

        try:
            import requests
            from urllib.parse import quote

            url = f"{self.email_server_url}/get_messages/{quote(self.agent_id)}?token={quote(token)}"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            messages = response.json().get("messages", [])
        except Exception as e:
            logger.debug("Mailbox scan skipped: %s", e)
            return

        for message in messages:
            message_id = str(message.get("message_id") or "")
            if not message_id or message_id in self._mailbox_signature_scan_ids:
                continue

            body = str(message.get("body", "") or "")
            subject = str(message.get("subject", "") or "")
            if "signed" not in subject.lower() and self._extract_signed_message(body) is None:
                continue

            self._mailbox_signature_scan_ids.add(message_id)
            self._maybe_submit_received_signature(message)

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
        block = CustomAgent._extract_labeled_block(body, patterns)
        if block is None:
            return []

        tail = CustomAgent._strip_wrapping_quotes(block.strip())
        parts = [part.strip() for part in re.split(r"[,\n;]+", tail) if part.strip()]
        if not parts:
            return []

        normalized_parts = [CustomAgent._strip_list_marker(part) for part in parts]
        if require_explicit:
            explicit = [part for part in normalized_parts if CustomAgent._looks_like_agent_id(part)]
            if len(explicit) != len(parts):
                return []
            return CustomAgent._normalize_agent_list(explicit)

        return [part for part in normalized_parts if part]

    @staticmethod
    def _extract_authorization_entries(body: str) -> Tuple[List[str], List[str]]:
        block = CustomAgent._extract_labeled_block(body, _AUTH_PATTERNS)
        if block is None:
            return [], []

        tail = CustomAgent._strip_wrapping_quotes(block.strip())
        parts = [part.strip() for part in re.split(r"[,\n;]+", tail) if part.strip()]
        if not parts:
            return [], []

        normalized_parts = [CustomAgent._strip_list_marker(part) for part in parts]
        explicit = [part for part in normalized_parts if CustomAgent._looks_like_agent_id(part)]
        descriptions = [part for part in normalized_parts if part not in explicit]
        return CustomAgent._normalize_agent_list(explicit), descriptions

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
    def _looks_like_signature_thread_reply(subject: str, body: str) -> bool:
        text = f"{subject}\n{body}".lower()
        if "signed_message_json" in text:
            return False
        return any(
            phrase in text
            for phrase in (
                "signature request",
                "request for signature",
                "sign this message",
                "please sign",
                "declined signature",
                "decline to sign",
                "cannot sign",
                "unable to sign",
            )
        )

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
    def _strip_list_marker(value: str) -> str:
        text = CustomAgent._strip_wrapping_quotes(value)
        text = re.sub(r"^\s*(?:[-*•]|\d+[.)]|[A-Za-z][.)])\s*", "", text)
        text = re.sub(r"\s*(?:[-–—:])\s*$", "", text).strip()
        return text

    @staticmethod
    def _extract_labeled_block(body: str, patterns: Sequence[str]) -> Optional[str]:
        lines = body.splitlines()
        for index, line in enumerate(lines):
            for pattern in patterns:
                match = re.search(pattern, line)
                if not match:
                    continue
                tail = match.group(1).strip()
                collected = [tail] if tail else []
                for continuation in lines[index + 1 :]:
                    stripped = continuation.strip()
                    if not stripped:
                        break
                    if stripped.startswith("**") and collected:
                        break
                    if re.match(r"^\d+[.)]\s+", stripped):
                        lower = stripped.lower()
                        if re.search(r"\b(you|request|requesting|authorized|authorised|sign|message|instructions|round)\b", lower):
                            break
                    collected.append(stripped)
                return "\n".join(collected) if collected else None
        return None

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lower()

    @classmethod
    def _content_tokens(cls, value: str) -> Set[str]:
        tokens = set(re.findall(r"[a-z0-9']+", value.lower()))
        return {token for token in tokens if token not in _STOPWORDS and len(token) > 2}
