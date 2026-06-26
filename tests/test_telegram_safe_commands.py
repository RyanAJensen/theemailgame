from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
    assert monitor._is_authorized_bot_to_bot_message(tester_message, "/tester_status") is True
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


def test_dashboard_text_puts_link_first():
    monitor = object.__new__(EmailGameMonitor)
    text = monitor._dashboard_text(url="https://example.trycloudflare.com/d/test-token/")
    assert "Public dashboard link" in text
    assert "https://example.trycloudflare.com/d/test-token/" in text
    assert text.index("Public dashboard link") < text.index("Open Race Control Dashboard")


def test_dashboard_url_text_mentions_view_only_sharing():
    monitor = object.__new__(EmailGameMonitor)
    text = monitor._dashboard_url_text()
    assert "Discord members" in text
    assert "view-only" in text or "view only" in text.lower()


def test_leaderboard_text_shows_score_deltas_for_every_visible_agent(monkeypatch):
    monitor = cast(Any, object.__new__(EmailGameMonitor))
    monitor._agent_name = "letlhogonolo_fanampe"
    monitor.leaderboard_state = SimpleNamespace(
        last_snapshot={
            "top5": [
                {"rank": 1, "agent_id": "alpha", "elo": 2010},
                {"rank": 2, "agent_id": "letlhogonolo_fanampe", "elo": 1860},
                {"rank": 3, "agent_id": "beta", "elo": 1790},
            ]
        }
    )
    monkeypatch.setattr(monitor_module, "_format_sast", lambda: "2026-06-26 18:00 SAST")
    monitor._fetch_leaderboard_data = lambda: (
        {
            "leaderboard": [
                {"rank": 1, "agent_id": "alpha", "elo": 2024},
                {"rank": 2, "agent_id": "letlhogonolo_fanampe", "elo": 1856},
                {"rank": 3, "agent_id": "beta", "elo": 1793},
            ]
        },
        "",
    )
    monitor._leaderboard_gap_to_rank = lambda entries, target_rank, me: "0"

    text = monitor._leaderboard_text()

    assert "Δ shows score change since the previous leaderboard poll." in text
    assert "alpha — 2024 (+14)" in text
    assert "letlhogonolo_fanampe ⭐ — 1856 (-4)" in text
    assert "beta — 1793 (+3)" in text


def test_restart_guard_blocks_in_game_restart():
    monitor = object.__new__(EmailGameMonitor)
    monitor._monitor_snapshot = lambda: {"phase": "in game", "in_game": True, "match_active": True}
    monitor._monitor_phase = lambda: "in game"

    allowed, phase, reason = monitor._restart_guard()

    assert allowed is False
    assert phase == "in game"
    assert "IN GAME" in reason


def test_codex_help_redirects_to_bridge():
    monitor = object.__new__(EmailGameMonitor)

    assert "CodexBridgePapzinBot" in monitor._dispatch_command("/codex_help", [])


def test_status_text_surfaces_snapshot_and_heartbeat(monkeypatch):
    monitor = object.__new__(EmailGameMonitor)
    monitor.state = SimpleNamespace(
        heartbeat_at="2026-06-25T08:00:00+00:00",
        status_snapshot={"phase": "waiting", "in_game": False, "match_active": False},
        phase="waiting",
        last_event="",
    )
    monitor.log_file = Path("/tmp/emailgame-live.log")
    monkeypatch.setattr(
        monitor,
        "_check_log_stream",
        lambda reconnect=False, refresh_capture=False, force_reconnect=False: SimpleNamespace(
            file_stale=False,
            age_seconds=12.0,
            pane_observed=False,
            pane_age_seconds=None,
            stale=False,
            reconnected=False,
            reconnect_error=None,
        ),
    )
    monkeypatch.setattr(monitor, "_branch_text", lambda: "main")
    monkeypatch.setattr(monitor, "_commit_text", lambda: "abc1234")
    monkeypatch.setattr(monitor, "_agent_process_running", lambda: True)
    monkeypatch.setattr(monitor_module, "_tmux_pane_pipe_connected", lambda session: True)
    monkeypatch.setattr(monitor, "_derive_phase", lambda: "waiting")
    monkeypatch.setattr(monitor, "_latest_significant_entry", lambda: SimpleNamespace(text="last log line", ts=None))
    monkeypatch.setattr(monitor, "_latest_round_number", lambda: "7")
    monkeypatch.setattr(monitor, "_latest_match_summary", lambda: None)
    monkeypatch.setattr(monitor, "_latest_structured_event", lambda: None)
    monkeypatch.setattr(monitor, "_latest_structured_event_of_type", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(monitor, "_latest_match_activity_event", lambda: None)
    monkeypatch.setattr(monitor, "_waiting_alone", lambda: False)
    monkeypatch.setattr(monitor, "_match_summary_line", lambda summary: "no match summary")
    monkeypatch.setattr(monitor, "_format_yes_no_unknown", lambda value: "yes" if value else "no")

    text = monitor._status_text()

    assert "🎮 <b>Email Game Status</b>" in text
    assert "• <b>Runtime snapshot phase</b>" in text
    assert "<code>waiting</code>" in text
    assert "• <b>Restart gate</b>" in text
    assert "open" in text
    assert "• <b>Monitor heartbeat freshness</b>" in text


def test_restart_guard_allows_waiting_restart():
    monitor = object.__new__(EmailGameMonitor)
    monitor._monitor_snapshot = lambda: {"phase": "waiting", "in_game": False, "match_active": False}
    monitor._monitor_phase = lambda: "waiting"

    allowed, phase, reason = monitor._restart_guard()

    assert allowed is True
    assert phase == "waiting"
    assert reason == ""
