"""AH3 — the second, independent channel. The dead-man must survive a Firebase ban of ntfy: a
priority alert must still reach Telegram when ntfy 429s, and Telegram must NOT carry routine chatter."""
from __future__ import annotations

from src.alerts.telegram_notifier import TelegramNotifier


def _tg(records):
    return TelegramNotifier(token="t", chat_id="c", post_fn=lambda tok, cid, text: records.append(text))


def test_telegram_sends_priority():
    rec = []
    _tg(rec).notify({"kind": "daily_liveness", "severity": "default", "message": "collector alive"})
    assert rec and "collector alive" in rec[0]


def test_telegram_sends_critical():
    rec = []
    _tg(rec).notify({"kind": "hard_critical_degrade", "severity": "urgent", "message": "engine HARD"})
    assert rec, "critical/urgent pages are priority -> the second channel must carry them"


def test_telegram_skips_routine():
    rec = []
    _tg(rec).notify({"kind": "degrade", "severity": "info", "message": "routine chatter"})
    assert not rec, "routine stays on ntfy; the second channel is priority-only"


def test_telegram_self_disables_without_config():
    rec = []
    TelegramNotifier(token="", chat_id="", post_fn=lambda *a: rec.append(a)).notify(
        {"kind": "daily_liveness", "severity": "default", "message": "x"})
    assert not rec


def test_second_channel_delivers_when_ntfy_is_429():
    """The whole point: ntfy banned -> Telegram STILL buzzes for the dead-man."""
    from src.alerts.ntfy_notifier import NtfyNotifier, NtfyRateLimited
    from src.alerts.email_dispatcher import CompositeNotifier

    def _ntfy_429(*a):
        raise NtfyRateLimited("429")
    tg_rec = []
    # ntfy with no cap (cap=object() would break); use a fresh notifier whose transport 429s
    ntfy = NtfyNotifier(topic="t", post_fn=_ntfy_429, cap=_NoCap())
    tg = TelegramNotifier(token="t", chat_id="c", post_fn=lambda tok, cid, text: tg_rec.append(text))
    push = CompositeNotifier(ntfy, tg)
    push.notify({"kind": "daily_liveness", "severity": "default", "message": "collector alive"})
    assert tg_rec, "ntfy 429 must not stop the independent Telegram path"


class _NoCap:
    """A cap that always allows (isolates this test from the shared-DB cap)."""
    def allow(self, kind, severity):
        return True, "test"
    def note_429(self, kind=None):
        pass
    def stats(self):
        return {}
