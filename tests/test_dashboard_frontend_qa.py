from __future__ import annotations

import json
from pathlib import Path

from scripts import dashboard_frontend_qa as qa


def test_redacts_protected_dashboard_urls():
    text = "open https://example.trycloudflare.com/d/abcdefghijklmnopqrstuvwxyz123456/ bot 123456:abcdefghijklmnopqrstuvwxyz12345"

    assert "[token]" in qa._redact(text)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in qa._redact(text)
    assert "123456:abcdefghijklmnopqrstuvwxyz12345" not in qa._redact(text)


def test_summary_payload_contains_viewport_checks():
    result = qa.QAResult(
        browser_available=True,
        screenshots=["dashboard_qa/screenshots/home-412x915.png"],
        score=95,
        readiness="excellent",
        main_issue="No major mobile layout issue detected.",
    )
    result.viewports.append(
        qa.ViewportResult(
            name="home-412x915.png",
            width=412,
            height=915,
            screenshot="dashboard_qa/screenshots/home-412x915.png",
            race_arena_exists=True,
            visual_arena_height=430,
            visual_overlay_coverage=0.1,
            visible_racer_count=5,
            need_chip_visible=True,
            our_racer_above_fold=True,
            race_numbers_above_fold=True,
            touch_reactive=True,
            you_tap_reaction=True,
            rival_tap_reaction=True,
            swipe_focus_reaction=True,
            long_press_slowmo=True,
            double_tap_reset=True,
        )
    )

    payload = qa._summary_payload(result)

    assert payload["qa_score"] == 95
    assert payload["screenshots_captured"] is True
    assert payload["viewports"][0]["race_arena_exists"] is True
    assert payload["viewports"][0]["visual_arena_height"] == 430
    assert payload["viewports"][0]["visual_overlay_coverage"] == 0.1
    assert payload["viewports"][0]["visible_racer_count"] == 5
    assert payload["viewports"][0]["need_chip_visible"] is True
    assert payload["viewports"][0]["our_racer_above_fold"] is True
    assert payload["viewports"][0]["race_numbers_above_fold"] is True
    assert payload["viewports"][0]["touch_reactive"] is True
    assert payload["viewports"][0]["you_tap_reaction"] is True
    assert payload["viewports"][0]["rival_tap_reaction"] is True
    assert payload["viewports"][0]["swipe_focus_reaction"] is True
    assert payload["viewports"][0]["long_press_slowmo"] is True
    assert payload["viewports"][0]["double_tap_reset"] is True
    assert payload["touch_reactive"] is True
    assert payload["you_pod_tap_reaction"] is True
    assert payload["rival_tap_reaction"] is True
    assert payload["swipe_focus_reaction"] is True
    assert payload["long_press_slowmo"] is True
    assert payload["double_tap_reset"] is True


def test_last_report_state_tracks_safe_fields(tmp_path, monkeypatch):
    state_path = tmp_path / "last_report_state.json"
    monkeypatch.setattr(qa, "LAST_REPORT_STATE_PATH", state_path)
    result = qa.QAResult(score=88, readiness="good")

    qa._write_last_report_state(
        result,
        "Dashboard QA complete.",
        "abc123",
        {"dashboard_qa/screenshots/home-412x915.png": "deadbeef"},
    )

    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["last_git_commit"] == "abc123"
    assert stored["last_screenshot_hashes"]["dashboard_qa/screenshots/home-412x915.png"] == "deadbeef"
    assert "token" not in json.dumps(stored).lower()


def test_last_send_result_is_redacted(tmp_path, monkeypatch):
    state_path = tmp_path / "last_send_result.json"
    monkeypatch.setattr(qa, "LAST_SEND_RESULT_PATH", state_path)

    qa._write_last_send_result(
        {
            "summary_sent": True,
            "screenshots_found": 3,
            "screenshots_sent": 3,
            "fallback_documents_sent": 0,
            "telegram_message_ids_received": True,
            "message_responses": [
                {
                    "method": "sendMessage",
                    "response": {
                        "ok": True,
                        "result": {
                            "message_id": 42,
                            "text": "open https://example.trycloudflare.com/d/abcdefghijklmnopqrstuvwxyz123456/",
                        },
                    },
                }
            ],
        }
    )

    stored = json.loads(state_path.read_text(encoding="utf-8"))
    stored_text = json.dumps(stored).lower()
    assert stored["summary_sent"] is True
    assert stored["message_responses"][0]["response"]["result"]["text"].endswith("/d/[token]/")
    assert "abcdefghijklmnopqrstuvwxyz123456" not in stored_text
    assert "telegram-token-redacted" not in stored_text


def test_send_result_lines_match_cli_contract():
    lines = qa._send_result_lines(
        {
            "summary_sent": True,
            "screenshots_found": 3,
            "screenshots_sent": 2,
            "fallback_documents_sent": 1,
            "telegram_message_ids_received": False,
        }
    )

    assert lines == [
        "summary sent: yes",
        "screenshots found: 3",
        "screenshots sent: 2",
        "fallback documents sent: 1",
        "telegram message ids received: no",
    ]


def test_telegram_summary_compares_previous_state():
    result = qa.QAResult(score=91, readiness="excellent", main_issue="No major mobile layout issue detected.")

    summary = qa._telegram_summary(
        result,
        {"last_git_commit": "old", "last_screenshot_hashes": {"shot.png": "old"}, "last_qa_score": 80},
        "new",
        {"shot.png": "new"},
    )

    assert "dashboard/QA commit changed" in summary
    assert "1 Android screenshot(s) changed" in summary
    assert "QA score is now 91" in summary


def test_telegram_summary_strips_double_punctuation():
    result = qa.QAResult(
        score=100,
        readiness="excellent",
        main_issue="No major mobile layout issue detected.",
        screenshots=["dashboard_qa/screenshots/home-412x915.png"],
    )

    summary = qa._telegram_summary(result, {}, "abc123", {"shot.png": "hash"})

    assert "detected.." not in summary
    assert summary.endswith("detected.")


def test_clipped_detector_ignores_below_fold_content():
    source = Path(qa.__file__).read_text(encoding="utf-8")

    assert "verticallyVisible = rect.bottom > 0 && rect.top < viewportHeight" in source
    assert "horizontallyClipped && verticallyVisible" in source
    assert "rect.bottom < 0 || rect.top > viewportHeight" not in source


def test_visual_first_qa_checks_are_enforced():
    result = qa.QAResult(browser_available=True)
    result.viewports.append(
        qa.ViewportResult(
            name="home-412x915.png",
            width=412,
            height=915,
            screenshot="dashboard_qa/screenshots/home-412x915.png",
            race_arena_exists=True,
            visual_arena_height=300,
            visual_overlay_coverage=0.5,
            card_overlaps_you_pod=True,
            visible_racer_count=2,
            need_chip_visible=False,
            our_racer_above_fold=True,
            race_numbers_above_fold=True,
        )
    )

    qa._score(result)

    assert result.score < 90
    assert result.main_issue == "visual race arena too short"


def test_touch_reactive_qa_checks_are_enforced():
    result = qa.QAResult(browser_available=True)
    result.viewports.append(
        qa.ViewportResult(
            name="home-412x915.png",
            width=412,
            height=915,
            screenshot="dashboard_qa/screenshots/home-412x915.png",
            race_arena_exists=True,
            visual_arena_height=430,
            visual_overlay_coverage=0.1,
            visible_racer_count=5,
            need_chip_visible=True,
            our_racer_above_fold=True,
            race_numbers_above_fold=True,
            touch_reactive=False,
            you_tap_reaction=False,
            rival_tap_reaction=False,
            swipe_focus_reaction=False,
            long_press_slowmo=False,
            double_tap_reset=False,
        )
    )

    qa._score(result)

    assert result.score < 90
    assert "touch" in result.main_issue


def test_telegram_send_writes_redacted_delivery_result(tmp_path, monkeypatch):
    qa.PROJECT_ROOT = tmp_path
    qa.QA_DIR = tmp_path / "dashboard_qa"
    qa.SCREENSHOT_DIR = qa.QA_DIR / "screenshots"
    qa.LAST_REPORT_STATE_PATH = qa.QA_DIR / "last_report_state.json"
    qa.LAST_SEND_RESULT_PATH = qa.QA_DIR / "last_send_result.json"
    qa.QA_DIR.mkdir(parents=True, exist_ok=True)
    qa.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    delivery_names = [
        "delivery-default.png",
        "delivery-you-focused.png",
        "delivery-you-dragged.png",
        "delivery-rival-gap-line.png",
    ]
    for name in delivery_names:
        (qa.SCREENSHOT_DIR / name).write_bytes(b"png")

    result = qa.QAResult(
        score=100,
        readiness="excellent",
        main_issue="No major mobile layout issue detected.",
        screenshots=["dashboard_qa/screenshots/home-412x915.png"],
        delivery_screenshots=[f"dashboard_qa/screenshots/{name}" for name in delivery_names],
    )

    monkeypatch.setattr(qa, "_load_private_env", lambda: None)
    monkeypatch.setattr(qa, "_dashboard_url", lambda: ("https://example.trycloudflare.com/d/test-token/", "public-url-file"))
    monkeypatch.setattr(qa, "_git_commit", lambda: "abc123")
    monkeypatch.setattr(qa, "_read_last_report_state", lambda: {})
    monkeypatch.setattr(qa, "_write_last_report_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(qa, "_screenshot_hashes", lambda *args, **kwargs: {"dashboard_qa/screenshots/home-412x915.png": "hash"})
    monkeypatch.setenv("EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz12345")
    monkeypatch.setenv("EMAIL_GAME_TEST_REPORT_CHAT_ID", "987654321")

    def fake_request(token, method, payload, timeout=20):
        assert method == "sendMessage"
        return True, {"ok": True, "result": {"message_id": 101, "text": "ok"}}, ""

    photo_calls = []

    def fake_photo(token, chat_id, path, caption=""):
        photo_calls.append((path.name, caption))
        if path.name == "delivery-you-focused.png":
            return False, {}, "photo failed"
        return True, {"ok": True, "result": {"message_id": 201, "file_id": "photo-file"}}, ""

    def fake_document(token, chat_id, path, caption=""):
        return True, {"ok": True, "result": {"message_id": 301, "file_id": "doc-file"}}, ""

    monkeypatch.setattr(qa, "_telegram_request", fake_request)
    monkeypatch.setattr(qa, "_telegram_send_photo", fake_photo)
    monkeypatch.setattr(qa, "_telegram_send_document", fake_document)

    sent, error, delivery_result = qa._telegram_send(result)

    assert sent is True
    assert error == ""
    assert delivery_result["summary_sent"] is True
    assert delivery_result["screenshots_found"] == 4
    assert delivery_result["screenshots_sent"] == 4
    assert delivery_result["fallback_documents_sent"] == 1
    assert delivery_result["telegram_message_ids_received"] is True
    assert len(photo_calls) == 4
    assert photo_calls[0][0] == "delivery-default.png"
    assert photo_calls[1][0] == "delivery-you-focused.png"

    stored = json.loads(qa.LAST_SEND_RESULT_PATH.read_text(encoding="utf-8"))
    assert stored["summary_sent"] is True
    assert stored["screenshots_found"] == 4
    assert stored["screenshots_sent"] == 4
    assert stored["fallback_documents_sent"] == 1
    assert stored["telegram_message_ids_received"] is True
    assert stored["message_responses"][0]["message_id"] == 101
    assert stored["message_responses"][1]["file"] == "delivery-default.png"
    assert stored["message_responses"][2]["method"] == "sendDocument"
