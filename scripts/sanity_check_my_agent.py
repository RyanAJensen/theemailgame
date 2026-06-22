#!/usr/bin/env python3
"""Offline sanity checks for my_agent.py.

This script exercises the deterministic parser and state handling without
starting the live Email Game runner.
"""

from __future__ import annotations

import json
from pathlib import Path
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
    agent.driver = SimpleNamespace(on_emails=lambda messages: None, message_log=[])

    events = {"sent": [], "signed": [], "submitted": []}

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
        ["dave"],
        ["erin"],
        auth_line="You may sign messages for",
    )
    agent.on_message_batch([round2])
    assert_equal(agent.current_round, 2, "round 2 tracking")
    assert_equal(agent.current_assigned_message, assigned_round2, "round 2 assigned message")
    assert_equal(len(agent._events["sent"]), 3, "round 2 request fanout")

    agent.on_message_batch([round1])
    assert_equal(len(agent._events["sent"]), 3, "stale previous-round moderator message ignored")

    delayed_reply = signed_payload_message("bob", assigned_round1)
    agent.on_message_batch([delayed_reply])
    assert_equal(len(agent._events["submitted"]), 2, "delayed previous-round signature accepted once")

    tampered_reply = signed_payload_message(
        "bob",
        assigned_round1 + "!",
        subject="Re: Signature request",
        include_marker=False,
    )
    agent.on_message_batch([tampered_reply])
    assert_equal(len(agent._events["submitted"]), 2, "tampered signed payload rejected")

    duplicate_round1 = request_message(
        "mallory",
        f"Please sign this message for me: {assigned_round1}",
    )
    agent.current_authorized_signers = ["carol"]
    agent.current_authorized_descriptions = []
    agent.current_authorization_resolved = ["carol"]
    agent.on_message_batch([duplicate_round1])
    assert_equal(len(agent._events["signed"]), 0, "unauthorized request should never be signed")
    assert_equal(len(agent._events["sent"]), 4, "unauthorized request gets one decline message")
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
