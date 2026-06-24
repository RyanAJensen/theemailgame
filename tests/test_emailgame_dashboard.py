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
    assert "Open Race Control Dashboard" in root.text

    health = client.get(f"/d/{token}/api/health")
    assert health.status_code == 200
    payload = health.json()
    assert "status" in payload
    assert "leaderboard" in payload
    assert "coach" in payload
    assert payload["status"]["monitor_running"] in {True, False}
