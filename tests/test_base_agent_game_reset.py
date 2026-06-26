from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

from src.base_agent import BaseAgent


def _make_agent() -> BaseAgent:
    agent = cast(Any, BaseAgent.__new__(BaseAgent))
    agent.agent_id = "letlhogonolo_fanampe"
    agent.moderator_agent = "moderator"
    agent.driver = SimpleNamespace(message_log=[{"role": "user", "content": "stale"}])
    agent.in_game = False
    agent.current_round = 0
    agent.can_send_reminder = False
    agent.last_message_time = datetime.now()
    agent.instructions_processed = 0
    agent.messages_sent = 0
    agent.current_instruction = None
    agent._seen_message_ids = {"old-message-id"}
    agent._submitted_signature_keys = {("alice", "letlhogonolo_fanampe", "old signed payload")}
    agent._redraw_pin = lambda: None
    agent._print_watch_banner = lambda: None
    return agent


def test_new_game_resets_cross_game_dedup_state_before_processing_first_round():
    agent = _make_agent()
    state = {}

    def on_new_game() -> None:
        state["dedup_cleared"] = (
            agent._seen_message_ids == set()
            and agent._submitted_signature_keys == set()
            and agent.driver.message_log == []
        )

    def on_message_batch(messages):
        state["messages"] = list(messages)

    agent.on_new_game = on_new_game
    agent.on_message_batch = on_message_batch

    agent._handle_message_batch(
        [
            {
                "message_id": "round-1-msg",
                "from": "moderator",
                "subject": "ROUND 1",
                "body": "**ROUND 1**\nWelcome back",
            }
        ]
    )

    assert state["dedup_cleared"] is True
    assert state["messages"][0]["message_id"] == "round-1-msg"
    assert agent._submitted_signature_keys == set()
    assert agent.current_round == 1
    assert agent.in_game is True


def test_clear_transcript_resets_dedup_state_and_counters():
    agent = _make_agent()
    agent.instructions_processed = 7
    agent.messages_sent = 3
    agent.current_instruction = "old instruction"
    agent.can_send_reminder = True

    agent.clear_transcript()

    assert agent.driver.message_log == []
    assert agent._seen_message_ids == set()
    assert agent._submitted_signature_keys == set()
    assert agent.instructions_processed == 0
    assert agent.messages_sent == 0
    assert agent.current_instruction is None
    assert agent.can_send_reminder is False
