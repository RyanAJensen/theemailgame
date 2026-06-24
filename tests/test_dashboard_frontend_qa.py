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
            our_racer_above_fold=True,
            race_numbers_above_fold=True,
        )
    )

    payload = qa._summary_payload(result)

    assert payload["qa_score"] == 95
    assert payload["screenshots_captured"] is True
    assert payload["viewports"][0]["our_racer_above_fold"] is True
    assert payload["viewports"][0]["race_numbers_above_fold"] is True


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
