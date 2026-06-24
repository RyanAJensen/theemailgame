from __future__ import annotations

from fastapi.testclient import TestClient

from scripts import emailgame_dashboard as dashboard


def test_dashboard_requires_token_and_serves_protected_api():
    client = TestClient(dashboard.app)
    token = dashboard._ensure_token_file()

    assert client.get("/").status_code == 403
    assert client.get("/api/dashboard").status_code == 403
    assert client.get("/d/wrong-token/").status_code == 403

    root = client.get(f"/d/{token}/")
    assert root.status_code == 200
    assert "Open Race Control Dashboard" not in root.text
    assert "Live Email Pod Race" in root.text
    assert "race-hero__title-strip" in root.text
    assert "race-chipbar" in root.text
    assert "race-chip is-hot" in root.text
    assert "race-hero__details" in root.text
    assert "race-canvas" in root.text
    assert "race-arena" in root.text
    assert "track-pod" in root.text
    assert "data-racer-visual" in root.text
    assert "race-state-banner" in root.text
    assert "race-ticker" in root.text
    assert "Next target" in root.text
    assert "table-scroll" in root.text
    assert "<th>Score</th>" in root.text
    assert "<th>Elo</th>" not in root.text
    assert "letlhogonolo_fanampe" in root.text

    health = client.get(f"/d/{token}/api/health")
    assert health.status_code == 200
    payload = health.json()
    assert "status" in payload
    assert "leaderboard" in payload
    assert "coach" in payload
    assert "race" in payload
    assert payload["status"]["monitor_running"] in {True, False}
