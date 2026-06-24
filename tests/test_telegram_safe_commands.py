from __future__ import annotations

from scripts.monitor_emailgame_telegram import EmailGameMonitor


def test_coach_is_authorized_for_tester_bot_only():
    monitor = object.__new__(EmailGameMonitor)

    tester_message = {"from": {"is_bot": True, "username": "EmailGameTesterBot"}}
    wrong_bot_message = {"from": {"is_bot": True, "username": "OtherBot"}}
    human_message = {"from": {"is_bot": False, "username": "EmailGameTesterBot"}}

    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/coach") is True
    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/startagent") is False
    assert monitor._is_authorized_bot_to_bot_message(wrong_bot_message, "/coach") is False
    assert monitor._is_authorized_bot_to_bot_message(human_message, "/coach") is False
