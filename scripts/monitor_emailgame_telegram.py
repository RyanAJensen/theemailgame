#!/usr/bin/env python3
"""Lightweight progress monitor for the Email Game agent.

Watches the live tmux log and emits Telegram updates only for important state
changes. If Telegram config is missing, it still prints the detected events to
stdout so the monitor can be verified locally.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Iterable, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = PROJECT_ROOT / "agent_logs" / "emailgame-live.log"
POLL_INTERVAL_SEC = 1.0
MAX_SEEN_LINES = 2000

load_dotenv(PROJECT_ROOT / ".env.local")


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _redact(text: str) -> str:
    text = re.sub(r"(token=)[^\s&]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(watch\?agent=[^&\s]+&token=)[^\s]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    return text


@dataclass(frozen=True)
class Event:
    kind: str
    text: str


class TelegramNotifier:
    def __init__(self) -> None:
        self.bot_token = _env(
            "EMAIL_GAME_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "TG_BOT_TOKEN",
        )
        self.chat_id = _env(
            "EMAIL_GAME_TELEGRAM_CHAT_ID",
            "TELEGRAM_CHAT_ID",
            "TG_CHAT_ID",
        )
        self.enabled = bool(self.bot_token and self.chat_id)
        self._warned_disabled = False
        self._warned_invalid_config = False
        self._last_failure: Optional[str] = None
        self._last_failure_count = 0

    def warn_if_disabled(self) -> None:
        if self.enabled or self._warned_disabled:
            return
        self._warned_disabled = True
        print(
            "[monitor] Telegram config not found. Fill "
            "EMAIL_GAME_TELEGRAM_BOT_TOKEN and EMAIL_GAME_TELEGRAM_CHAT_ID in .env.local."
        )

    def _warn_if_config_looks_invalid(self) -> None:
        if self._warned_invalid_config or not self.enabled:
            return
        token_shape_ok = bool(re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", self.bot_token))
        chat_id_shape_ok = bool(re.match(r"^-?\d+$", self.chat_id) or self.chat_id.startswith("@"))
        if token_shape_ok and chat_id_shape_ok:
            return
        self._warned_invalid_config = True
        print(
            "[monitor] Telegram config looks malformed: "
            f"token_shape_ok={token_shape_ok} chat_id_shape_ok={chat_id_shape_ok}"
        )

    def _warn_about_failure(self, message: str) -> None:
        if message == self._last_failure:
            self._last_failure_count += 1
            if self._last_failure_count == 2:
                print(f"[monitor] Repeating Telegram failure suppressed: {message}")
            return
        self._last_failure = message
        self._last_failure_count = 1
        print(f"[monitor] {message}")

    def send(self, message: str) -> bool:
        message = message.strip()
        if not message:
            return False
        if not self.enabled:
            self.warn_if_disabled()
            print(f"[monitor] {message}")
            return False
        self._warn_if_config_looks_invalid()

        payload = urlencode({"chat_id": self.chat_id, "text": message}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                return 200 <= getattr(response, "status", 200) < 300
        except Exception as exc:
            body = ""
            code = getattr(exc, "code", None)
            if hasattr(exc, "read"):
                try:
                    body = exc.read().decode("utf-8", "replace").strip()
                except Exception:
                    body = ""
            failure = f"Telegram send failed"
            if code is not None:
                failure += f" ({code})"
            if body:
                failure += f": {body}"
            else:
                failure += f": {exc}"
            if code == 400 and "chat not found" in body.lower():
                failure = (
                    "Telegram send failed (400): chat not found. "
                    "Verify EMAIL_GAME_TELEGRAM_CHAT_ID and that the bot was started in that chat."
                )
            self._warn_about_failure(failure)
            return False


class EmailGameMonitor:
    def __init__(self, log_file: Path) -> None:
        self.log_file = log_file
        self.notifier = TelegramNotifier()
        self._line_buffer: Deque[str] = deque(maxlen=MAX_SEEN_LINES)
        self._sent_event_keys: set[Tuple[str, str]] = set()
        self._offset = 0
        self._agent_name = _env("EMAIL_GAME_AGENT_NAME") or "letlhogonolo_fanampe"

    def run(self) -> None:
        print(f"[monitor] watching {self.log_file}")
        self.notifier.warn_if_disabled()
        self._sync_start_offset()
        while True:
            self._poll_once()
            time.sleep(POLL_INTERVAL_SEC)

    def _sync_start_offset(self) -> None:
        if not self.log_file.exists():
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self._offset = 0
            return
        self._offset = self.log_file.stat().st_size

    def _poll_once(self) -> None:
        if not self.log_file.exists():
            return

        size = self.log_file.stat().st_size
        if size < self._offset:
            self._offset = 0

        if size == self._offset:
            return

        with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()

        for raw_line in chunk.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in self._line_buffer:
                continue
            self._line_buffer.append(line)
            event = self._classify(line)
            if event:
                self._notify(event)

    def _notify(self, event: Event) -> None:
        key = (event.kind, event.text)
        if key in self._sent_event_keys:
            return
        self._sent_event_keys.add(key)
        self.notifier.send(event.text)

    def _classify(self, line: str) -> Optional[Event]:
        clean = _redact(line)

        if "Starting custom agent" in line or "Loaded CustomAgent" in line:
            return Event("started", f"Email Game agent started: {self._agent_name}")

        queue_match = re.search(r"Joined matchmaking queue \(position (\d+)\)", line)
        if queue_match:
            return Event("queue", f"Agent joined matchmaking queue at position {queue_match.group(1)}")

        if "✅ Match found - game starting!" in line or "🎮 IN GAME - Round 1" in line:
            return Event("match_started", f"Match started for {self._agent_name}")

        if "Round " in line and " moderator message" not in line and "IN GAME - Round" in line:
            round_match = re.search(r"Round\s+(\d+)", line)
            if round_match:
                return Event("moderator", f"Moderator message received for round {round_match.group(1)}")

        if "Sent signature request to " in line:
            agent = line.rsplit("Sent signature request to ", 1)[-1].strip()
            return Event("signature_request", f"Signature request sent to {agent}")

        if "Signed request from " in line:
            agent = line.rsplit("Signed request from ", 1)[-1].strip()
            return Event("signed", f"Signature received and signed request sent for {agent}")

        if "Submitted received signature from " in line:
            agent = line.rsplit("Submitted received signature from ", 1)[-1].strip()
            return Event("signature_submitted", f"Received signature from {agent} and submitted it")

        if "Game over - between matches now" in line:
            return Event("game_finished", "Game finished; agent is back between matches")

        if (
            "disconnected" in line.lower()
            or "connection dropped" in line.lower()
            or "connection error" in line.lower()
        ):
            return Event("disconnected", f"Agent disconnected or reconnected: {clean}")

        error_markers = (
            "Traceback",
            "LLM consumer error",
            "on_new_game() error",
            "Error handling messages",
            "Could not reach the game server",
            "Could not join",
            "LLM Driver error",
            "Failed to import",
            "Failed to send message",
        )
        if any(marker in line for marker in error_markers) or re.search(r"\bERROR\b", line):
            return Event("error", f"Agent error: {clean}")

        return None


def main() -> int:
    monitor = EmailGameMonitor(LOG_FILE)
    try:
        monitor.run()
    except KeyboardInterrupt:
        print("\n[monitor] shutting down")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
