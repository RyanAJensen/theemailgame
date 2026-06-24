from __future__ import annotations

import json
import logging
from pathlib import Path
from io import StringIO
from types import SimpleNamespace

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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


def moderator_message(round_number: int, assigned: str, request_targets, auth_targets, request_label="You must REQUEST signatures from these agents", auth_label="You are AUTHORIZED to sign messages for these agents", request_joiner=", ", auth_joiner=", "):
    request_lines = "\n".join(f"{idx + 1}. {target}" for idx, target in enumerate(request_targets))
    auth_lines = "\n".join(f"- {target}" for target in auth_targets)
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
            f"1. {request_label}: {request_lines if request_lines else request_joiner.join(request_targets)}\n"
            f"2. {auth_label}: {auth_lines if auth_lines else auth_joiner.join(auth_targets)}\n"
        ),
    }


def request_message(sender: str, body: str):
    return {
        "from": sender,
        "to": AGENT_ID,
        "subject": "Signature request",
        "body": body,
    }


def signed_payload_message(sender: str, original_message: str, signed_for: str = AGENT_ID):
    signed_message = {
        "original_message": original_message,
        "signature": "ZmFrZS1zaWduYXR1cmU=",
        "signer": sender,
        "signed_for": signed_for,
        "timestamp": "2026-06-22T00:00:00",
        "signature_type": "rsa_pss_sha256",
    }
    return {
        "from": sender,
        "to": AGENT_ID,
        "subject": "Signed Message",
        "body": f"Here you go.\n\nSIGNED_MESSAGE_JSON:{json.dumps(signed_message, separators=(',', ':'))}",
    }


def test_round_parsing_fans_out_across_bulleted_and_numbered_targets():
    agent = make_agent()
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    logger = logging.getLogger("my_agent")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        round1_message = moderator_message(1, "Alpha message.", ["alice", "bob"], ["carol"])
        round2_message = moderator_message(2, "Bravo message.", ["dave", "erin"], ["erin"])
        round3_message = moderator_message(3, "Charlie message.", ["frank", "george"], ["frank"])

        agent.on_message_batch([round1_message])
        assert agent.current_round == 1
        assert agent.current_request_targets == ["alice", "bob"]
        assert len(agent._events["sent"]) == 2
        assert agent._pending_signature_requests["Alpha message."] == {"alice", "bob"}

        agent.on_message_batch([round2_message])
        assert agent.current_round == 2
        assert agent.current_request_targets == ["dave", "erin"]
        assert len(agent._events["sent"]) == 4
        assert agent._pending_signature_requests["Bravo message."] == {"dave", "erin"}

        stale_round1_reply = signed_payload_message("alice", "Alpha message.")
        agent.on_message_batch([stale_round1_reply])
        assert len(agent._events["submitted"]) == 0

        agent.on_message_batch([round3_message])
        assert agent.current_round == 3
        assert agent.current_request_targets == ["frank", "george"]
        assert len(agent._events["sent"]) == 6
        assert agent._pending_signature_requests["Charlie message."] == {"frank", "george"}

        round3_reply = signed_payload_message("frank", "Charlie message.")
        agent.on_message_batch([round3_reply])
        assert len(agent._events["submitted"]) == 1

        duplicate_request = request_message("mallory", "Please sign this message for me: Charlie message.")
        agent.current_authorized_signers = ["carol"]
        agent.current_authorized_descriptions = []
        agent.current_authorization_resolved = ["carol"]
        sent_before = len(agent._events["sent"])
        agent.on_message_batch([duplicate_request])
        assert len(agent._events["sent"]) == sent_before + 1
        assert agent._events["sent"][-1]["to_agent"] == "mallory"

        second_duplicate = request_message("mallory", "Please sign this message for me: Charlie message.")
        agent.on_message_batch([second_duplicate])
        assert len(agent._events["sent"]) == sent_before + 1

        log_text = log_stream.getvalue()
        assert "Round 2: assigned=True request_targets=2" in log_text
        assert "Round 3: assigned=True request_targets=2" in log_text
        assert "Skipped because unauthorized: declined request from mallory in round 3" in log_text
    finally:
        logger.removeHandler(handler)

