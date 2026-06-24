from __future__ import annotations

from pathlib import Path

from scripts import telegram_token_audit as audit


def test_mask_chat_id_hides_middle_digits():
    assert audit._mask_chat_id("8828002971") == "882…971"
    assert audit._mask_chat_id("") == "missing"


def test_all_equal_requires_non_empty_values():
    assert audit._all_equal(["123", "123", "123"]) is True
    assert audit._all_equal(["123", "456"]) is False
    assert audit._all_equal(["", ""]) is False


def test_file_mentions_requires_all_needles(tmp_path):
    path = tmp_path / "launch.sh"
    path.write_text('source "$ENV_FILE"\nCODEX_BRIDGE_TELEGRAM_BOT_TOKEN=ok\n', encoding="utf-8")

    assert audit._file_mentions(path, 'source "$ENV_FILE"', "CODEX_BRIDGE_TELEGRAM_BOT_TOKEN") is True
    assert audit._file_mentions(path, "missing") is False
