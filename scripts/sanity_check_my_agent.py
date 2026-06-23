#!/usr/bin/env python3
"""Offline sanity checks for my_agent.py.

This script exercises the deterministic parser and state handling without
starting the live Email Game runner.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from io import StringIO
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]

import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from my_agent import CustomAgent


AGENT_ID = "letlhogonolo_fanampe"


def make_agent():
    agent = object.__new__(CustomAgent)
    CustomAgent._reset_game_state(agent)
    agent.agent_id = AGENT_ID
    agent.username = AGENT_ID
    agent.moderator_agent = "moderator"
    agent.in_game = True
    agent.messages_sent = 0
    agent.current_instruction = None
    events = {"sent": [], "signed": [], "submitted": [], "fallback": []}
    agent.driver = SimpleNamespace(on_emails=lambda messages: events["fallback"].extend(messages), message_log=[])

    def send_message(**kwargs):
        events["sent"].append(kwargs)
        agent.messages_sent += 1
        return {"success": True, "message_id": f"msg-{len(events['sent'])}"}

    def sign_and_respond(**kwargs):
        events["signed"].append(kwargs)
        return {"success": True, "message_id": f"signed-{len(events['signed'])}"}

    def submit_signature(signed_message):
        events["submitted"].append(signed_message)
        return {"success": True, "message_id": f"submitted-{len(events['submitted'])}"}

    agent.send_message = send_message
    agent.sign_and_respond = sign_and_respond
    agent.submit_signature = submit_signature
    agent._events = events
    return agent


def moderator_message(round_number: int, assigned: str, request_targets, auth_targets, auth_line="You are AUTHORIZED to sign messages for these agents"):
    request_text = ", ".join(request_targets)
    auth_text = ", ".join(auth_targets)
    return {
        "message_id": f"moderator-round-{round_number}-{assigned}",
        "from": "moderator",
        "to": AGENT_ID,
        "subject": f"📢 The Email Game – Round {round_number} Instructions for {AGENT_ID.title()}",
        "body": (
            f"Welcome, {AGENT_ID.title()}!\n\n"
            f"**ROUND {round_number}** - Message signing and verification round.\n\n"
            "**Your Assigned Message:**\n"
            f"You must get signatures for this EXACT message: \"{assigned}\"\n\n"
            "**Your Signing Requirements:**\n"
            f"1. You must REQUEST signatures from these agents: {request_text}\n"
            f"2. {auth_line}: {auth_text}\n"
        ),
    }


def request_message(sender: str, body: str):
    return {
        "from": sender,
        "to": AGENT_ID,
        "subject": "Signature request",
        "body": body,
    }


def thread_reply(sender: str, subject: str, body: str = "Thanks for handling that."):
    return {
        "from": sender,
        "to": AGENT_ID,
        "subject": subject,
        "body": body,
    }


def signed_payload_message(
    sender: str,
    original_message: str,
    signed_for: str = AGENT_ID,
    subject: str = "Signed Message",
    body_prefix: str = "Here you go.",
    include_marker: bool = True,
):
    signed_message = {
        "original_message": original_message,
        "signature": "ZmFrZS1zaWduYXR1cmU=",
        "signer": sender,
        "signed_for": signed_for,
        "timestamp": "2026-06-22T00:00:00",
        "signature_type": "rsa_pss_sha256",
    }
    if include_marker:
        body = f"{body_prefix}\n\nSIGNED_MESSAGE_JSON:{json.dumps(signed_message, separators=(',', ':'))}"
    else:
        body = json.dumps(signed_message)
    return {
        "from": sender,
        "to": AGENT_ID,
        "subject": subject,
        "body": body,
    }


def assert_equal(left, right, message):
    if left != right:
        raise AssertionError(f"{message}: expected {right!r}, got {left!r}")


def main() -> None:
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    logging.getLogger("my_agent").addHandler(handler)
    logging.getLogger("my_agent").setLevel(logging.INFO)

    agent = make_agent()

    assigned_round1 = "Alpha  beta; keep the exact spacing."
    round1 = moderator_message(
        1,
        assigned_round1,
        ["alice", "bob"],
        ["carol"],
    )
    agent.on_message_batch([round1])

    assert_equal(agent.current_round, 1, "round 1 tracking")
    assert_equal(agent.current_assigned_message, assigned_round1, "exact assigned message preservation")
    assert_equal(agent.current_request_targets, ["alice", "bob"], "request target parsing")
    assert_equal(len(agent._events["sent"]), 2, "round 1 request fanout")
    assert_equal(agent._pending_signature_requests[assigned_round1], {"alice", "bob"}, "pending request tracking")

    agent.on_message_batch([round1])
    assert_equal(len(agent._events["sent"]), 2, "duplicate moderator message suppression")

    reply1 = signed_payload_message("alice", assigned_round1, subject="Re: Signature Request")
    agent.on_message_batch([reply1])
    assert_equal(len(agent._events["submitted"]), 1, "signature submission from pending request")
    reply1_duplicate_subject = signed_payload_message(
        "alice",
        assigned_round1,
        subject="Signature Request Response",
        body_prefix="Thanks!",
        include_marker=False,
    )
    agent.on_message_batch([reply1_duplicate_subject])
    assert_equal(len(agent._events["submitted"]), 1, "duplicate signed payload suppression")

    assigned_round2 = "Bravo message with newline-safe handling."
    round2 = moderator_message(
        2,
        assigned_round2,
        ["dave", "erin"],
        ["erin"],
        auth_line="You may sign messages for",
    )
    agent._seen_message_ids.add(round2["message_id"])
    agent.on_message_batch([round2])
    assert_equal(agent.current_round, 2, "round 2 tracking")
    assert_equal(agent.current_assigned_message, assigned_round2, "round 2 assigned message")
    assert_equal(len(agent._events["sent"]), 4, "round 2 request fanout")

    agent.on_message_batch([round1])
    assert_equal(len(agent._events["sent"]), 4, "stale previous-round moderator message ignored")

    delayed_reply = signed_payload_message("bob", assigned_round1)
    agent.on_message_batch([delayed_reply])
    assert_equal(len(agent._events["submitted"]), 1, "stale round 1 signature ignored during round 2")

    round2_reply = signed_payload_message("dave", assigned_round2, subject="Signature Request Response")
    agent.on_message_batch([round2_reply])
    assert_equal(len(agent._events["submitted"]), 2, "round 2 signature request response submitted")

    tampered_reply = signed_payload_message(
        "bob",
        assigned_round1 + "!",
        subject="Re: Signature request",
        include_marker=False,
    )
    agent.on_message_batch([tampered_reply])
    assert_equal(len(agent._events["submitted"]), 2, "tampered signed payload rejected")

    assigned_round3 = "Charlie message for final round handling."
    round3 = moderator_message(
        3,
        assigned_round3,
        ["frank", "george"],
        ["alice"],
    )
    agent.on_message_batch([round3])
    assert_equal(agent.current_round, 3, "round 3 tracking")
    assert_equal(agent.current_assigned_message, assigned_round3, "round 3 assigned message")
    assert_equal(len(agent._events["sent"]), 6, "round 3 request fanout")

    stale_round2_reply = signed_payload_message("erin", assigned_round2)
    agent.on_message_batch([stale_round2_reply])
    assert_equal(len(agent._events["submitted"]), 2, "stale round 2 reply ignored during round 3")

    round3_reply = signed_payload_message("frank", assigned_round3, subject="Re: Signature Request")
    agent.on_message_batch([round3_reply])
    assert_equal(len(agent._events["submitted"]), 3, "round 3 signed reply submitted once")

    round3_duplicate = signed_payload_message("frank", assigned_round3, subject="Signature Request Response")
    agent.on_message_batch([round3_duplicate])
    assert_equal(len(agent._events["submitted"]), 3, "round 3 duplicate signed reply ignored after submission")

    missing_signer_reply = signed_payload_message("mallory", assigned_round3)
    agent.on_message_batch([missing_signer_reply])
    assert_equal(len(agent._events["submitted"]), 3, "round 3 missing signer skipped")

    log_text = log_stream.getvalue()
    if "Signed payload decision: current_round=3 signer=frank assignment_matched=yes submit_attempted=yes submit_succeeded=yes skipped_reason=none" not in log_text:
        raise AssertionError("submitted signature decision log missing for coach metrics")
    if "skipped_reason=duplicate" not in log_text:
        raise AssertionError("duplicate signed payload decision log missing")
    if "skipped_reason=stale" not in log_text:
        raise AssertionError("stale signed payload decision log missing")
    if "missing required signer: mallory" not in log_text:
        raise AssertionError("missing-required-signer decision log does not include signer")

    focused = make_agent()
    focused_round3_message = "Delta message for sign-then-decline prevention."
    focused_round3 = moderator_message(
        3,
        focused_round3_message,
        ["house_bot_3", "house_bot_2"],
        ["house_bot_2"],
    )
    focused.on_message_batch([focused_round3])
    assert_equal(len(focused._events["sent"]), 2, "focused round 3 request fanout")
    assert_equal(len(focused._events["fallback"]), 0, "moderator deterministic handling avoids fallback")

    authorized_request = request_message(
        "house_bot_2",
        "Please sign this message for me: Authorized inbound request.",
    )
    focused.on_message_batch([authorized_request])
    assert_equal(len(focused._events["signed"]), 1, "authorized request signed once")
    assert_equal(len(focused._events["fallback"]), 0, "authorized request not passed to fallback")

    focused.on_message_batch([authorized_request])
    assert_equal(len(focused._events["signed"]), 1, "duplicate authorized request not signed twice")
    assert_equal(len(focused._events["fallback"]), 0, "duplicate authorized request not passed to fallback")

    signed_request_followup = thread_reply(
        "house_bot_2",
        "Declined Signature Request",
        "Re: request for signature. No further action needed.",
    )
    focused.on_message_batch([signed_request_followup])
    assert_equal(len(focused._events["sent"]), 2, "signed requester follow-up does not trigger decline")
    assert_equal(len(focused._events["fallback"]), 0, "signed requester follow-up not passed to fallback")

    unauthorized_request = request_message(
        "house_bot_1",
        "Please sign this message for me: Unauthorized inbound request.",
    )
    focused.on_message_batch([unauthorized_request])
    assert_equal(len(focused._events["sent"]), 3, "unauthorized request declined once")
    focused.on_message_batch([unauthorized_request])
    assert_equal(len(focused._events["sent"]), 3, "duplicate unauthorized request not declined twice")
    assert_equal(len(focused._events["fallback"]), 0, "unauthorized request not passed to fallback")

    focused_signature = signed_payload_message("house_bot_3", focused_round3_message)
    focused.on_message_batch([focused_signature])
    assert_equal(len(focused._events["submitted"]), 1, "focused round 3 valid signed payload submitted")
    focused.on_message_batch([focused_signature])
    assert_equal(len(focused._events["submitted"]), 1, "focused round 3 duplicate payload skipped")
    assert_equal(
        focused._pending_signature_requests[focused_round3_message],
        {"house_bot_2"},
        "missing house_bot_2 remains pending after house_bot_3 submission",
    )

    log_text = log_stream.getvalue()
    if "ignored signature-thread follow-up from already signed requester house_bot_2 in round 3" not in log_text:
        raise AssertionError("signed requester follow-up skip log missing")
    if "Skipped because duplicate: unauthorized request already declined from house_bot_1 in round 3" not in log_text:
        raise AssertionError("duplicate unauthorized decline skip log missing")

    duplicate_round1 = request_message(
        "mallory",
        f"Please sign this message for me: {assigned_round1}",
    )
    agent.current_authorized_signers = ["carol"]
    agent.current_authorized_descriptions = []
    agent.current_authorization_resolved = ["carol"]
    sent_before_decline = len(agent._events["sent"])
    agent.on_message_batch([duplicate_round1])
    assert_equal(len(agent._events["signed"]), 0, "unauthorized request should never be signed")
    assert_equal(len(agent._events["sent"]), sent_before_decline + 1, "unauthorized request gets one decline message")
    assert_equal(agent._events["sent"][-1]["to_agent"], "mallory", "unauthorized request decline target")

    agent.on_new_game()
    assert_equal(agent.current_round, 0, "new game round reset")
    assert_equal(agent.current_assigned_message, None, "new game assigned reset")
    assert_equal(agent._pending_signature_requests, {}, "new game pending reset")
    assert_equal(agent._processed_moderator_keys, set(), "new game moderator dedupe reset")
    assert_equal(agent._sender_history, {}, "new game sender history reset")

    print("my_agent offline sanity checks passed")


if __name__ == "__main__":
    main()
