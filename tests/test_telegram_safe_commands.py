from __future__ import annotations

from scripts import monitor_emailgame_telegram as monitor_module
from scripts.monitor_emailgame_telegram import EmailGameMonitor


def test_coach_is_authorized_for_tester_bot_only():
    monitor = object.__new__(EmailGameMonitor)

    tester_message = {"from": {"is_bot": True, "username": "EmailGameTesterBot"}}
    wrong_bot_message = {"from": {"is_bot": True, "username": "OtherBot"}}
    human_message = {"from": {"is_bot": False, "username": "EmailGameTesterBot"}}

    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/coach") is True
    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/dashboard") is True
    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/dashboard_url") is True
    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/dashboard_refresh") is True
    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/startagent") is False
    assert monitor._is_authorized_bot_to_bot_message(wrong_bot_message, "/coach") is False
    assert monitor._is_authorized_bot_to_bot_message(human_message, "/coach") is False


def test_dashboard_reply_markup_includes_button_when_link_exists(monkeypatch):
    monitor = object.__new__(EmailGameMonitor)
    monkeypatch.setattr(
        monitor_module,
        "_read_text_file",
        lambda path: "https://example.trycloudflare.com/d/test-token/" if str(path).endswith("emailgame-dashboard-url.txt") else "",
    )

    reply_markup = monitor._dashboard_reply_markup(url="https://example.trycloudflare.com/d/test-token/")
    assert reply_markup is not None
    assert reply_markup["inline_keyboard"][0][0]["text"] == "Open Race Control Dashboard"
    assert reply_markup["inline_keyboard"][0][0]["url"] == "https://example.trycloudflare.com/d/test-token/"
