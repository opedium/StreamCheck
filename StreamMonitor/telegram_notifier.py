#!/usr/bin/env python3
# coding=utf-8
"""Telegram notification for cookie health events.

Fire-and-forget with state-transition deduplication to prevent spam.
Telegram failures never propagate — this is best-effort alerting.
"""

import os

import requests
from dotenv import load_dotenv


class TelegramNotifier:
    """Send cookie health alerts via Telegram Bot API."""

    def __init__(self, bot_token=None, chat_id=None):
        if bot_token is None or chat_id is None:
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            if os.path.exists(env_path):
                load_dotenv(env_path)
            bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._last_state = None  # for dedup

    @property
    def configured(self):
        return bool(self.bot_token and self.chat_id)

    def send(self, message, state=None):
        """Send a Telegram message.

        Args:
            message: Text to send.
            state: Optional state label.  If provided and matches the
                   last sent state, the message is skipped (dedup).
                   Use None to force-send regardless of dedup.

        Returns:
            True if sent, False if skipped or failed.
        """
        if state is not None:
            if state == self._last_state:
                return False
            self._last_state = state

        if not self.configured:
            print(
                f"[Telegram] Not configured — message not sent: {message}",
                flush=True,
            )
            return False

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            if resp.status_code == 200:
                print(f"[Telegram] Sent: {message}", flush=True)
                return True
            else:
                print(
                    f"[Telegram] HTTP {resp.status_code}: {resp.text[:200]}",
                    flush=True,
                )
                # Reset state on failure so next attempt retries
                self._last_state = None
                return False
        except Exception as e:
            print(f"[Telegram] Error: {e}", flush=True)
            self._last_state = None
            return False
