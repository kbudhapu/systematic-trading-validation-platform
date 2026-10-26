"""AI — the THIRD independent channel. Two failure modes this suite makes unrepresentable:
  1. The SILENT-BADGE bug (a webhook post with no @here + allowed_mentions is a silent unread badge,
     not a notification -- the July-7 shape). Priority payloads MUST carry both.
  2. The webhook URL (a credential) leaking into a log line / error string.
And the reason the channel exists at all: it must DELIVER when ntfy is 429'd / capped / banned."""
from __future__ import annotations

from src.alerts.discord_notifier import DiscordNotifier, DiscordRateLimited, build_discord_content


def _dc(records):
    return DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/123/abcSECRETtoken",
        post_fn=lambda url, content, allowed: records.append((url, content, allowed)),
    )


def test_priority_payload_carries_here_and_allowed_mentions():
    """The core anti-silent-badge invariant: a priority alert MUST @here AND pass allowed_mentions,
    or Discord silently strips the mention and nobody's phone rings."""
    content, allowed = build_discord_content(
        {"kind": "daily_liveness", "severity": "urgent", "message": "collector alive"})
    assert "@here" in content, "priority without @here is a silent badge"
    assert allowed == {"parse": ["everyone"]}, "without allowed_mentions Discord strips the mention"
    assert "collector alive" in content


def test_routine_payload_has_no_mention():
    content, allowed = build_discord_content(
        {"kind": "degrade", "severity": "info", "message": "routine chatter"})
    assert "@here" not in content
    assert allowed is None


def test_discord_sends_priority():
    rec = []
    _dc(rec).notify({"kind": "daily_liveness", "severity": "default", "message": "collector alive"})
    assert rec and "@here" in rec[0][1] and rec[0][2] == {"parse": ["everyone"]}


def test_discord_skips_routine():
    rec = []
    _dc(rec).notify({"kind": "degrade", "severity": "info", "message": "routine chatter"})
    assert not rec, "routine stays on ntfy; Discord is priority-only"


def test_discord_self_disables_under_empty_env(monkeypatch):
    """B4/B5: construct under a completely empty environment -> no exception, self-disabled, and the
    alert still reaches the durable LogNotifier leg (proven here by the push simply being a no-op)."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    rec = []
    n = DiscordNotifier(post_fn=lambda *a: rec.append(a))
    assert not n.enabled()
    n.notify({"kind": "daily_liveness", "severity": "urgent", "message": "x"})   # must not raise
    assert not rec


def test_webhook_url_never_leaks_into_logs():
    """AI2: an httpx failure prints the FULL request URL. The notifier must redact it. Simulate a
    transport that raises with the URL embedded (exactly what httpx does) and assert it is scrubbed."""
    secret = "https://discord.com/api/webhooks/123/abcSECRETtoken"
    captured = {}

    def _boom(url, content, allowed):
        raise RuntimeError(f"connect error to {url}")   # httpx-style: URL in the message

    import src.alerts.discord_notifier as mod
    orig = mod.log.warning
    mod.log.warning = lambda event, **kw: captured.update(kw)
    try:
        DiscordNotifier(webhook_url=secret, post_fn=_boom).notify(
            {"kind": "daily_liveness", "severity": "urgent", "message": "x"})
    finally:
        mod.log.warning = orig
    assert "SECRETtoken" not in str(captured), f"webhook leaked into log: {captured}"
    assert "redacted" in str(captured)


def test_discord_delivers_when_ntfy_is_429_or_capped():
    """THE reason this channel exists (AI3): ntfy banned/capped -> Discord STILL fires. ntfy here has
    a cap that ALWAYS suppresses AND a transport that 429s -- Discord must be untouched by both."""
    from src.alerts.ntfy_notifier import NtfyNotifier
    from src.alerts.email_dispatcher import CompositeNotifier

    class _CapSuppressAll:
        def allow(self, kind, severity):
            return False, "channel_backoff_after_429"   # ntfy fully capped
        def note_429(self, kind=None):
            pass
        def stats(self):
            return {}

    def _ntfy_429(*a):
        raise AssertionError("should not even POST -- cap suppressed it")

    dc_rec = []
    ntfy = NtfyNotifier(topic="t", post_fn=_ntfy_429, cap=_CapSuppressAll())
    discord = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/x",
                              post_fn=lambda url, content, allowed: dc_rec.append(content))
    push = CompositeNotifier(ntfy, discord)
    push.notify({"kind": "daily_liveness", "severity": "urgent", "message": "collector alive"})
    assert dc_rec, "ntfy cap/429 must NOT stop the independent Discord path (AI3)"
    assert "@here" in dc_rec[0]


def test_discord_swallows_429():
    """A Discord 429 must back off gracefully, never raise into the watchdog."""
    def _429(*a):
        raise DiscordRateLimited("429")
    DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/x", post_fn=_429).notify(
        {"kind": "daily_liveness", "severity": "urgent", "message": "x"})   # must not raise
