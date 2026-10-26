"""BOD/EOD email reporting via SMTP."""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

import structlog

from src.config import AppConfig
from src.persistence.db import export_csv, get_daily_pnl_rows, get_recent_trades

log = structlog.get_logger()


def _send_email(config: AppConfig, subject: str, body: str) -> None:
    """
    Send an email via configured SMTP.

    Falls back to printing the body when SMTP is not configured.
    """
    if not config.smtp_host or not config.email_to:
        log.warning("email_skipped", reason="SMTP or recipient not configured")
        print(f"\n--- {subject} ---\n{body}\n")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.email_from
    msg["To"] = config.email_to

    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.starttls()
        if config.smtp_user:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(msg)

    log.info("email_sent", subject=subject, to=config.email_to)


def send_bod_email(
    config: AppConfig, positions_summary: str = "", equity: float = 0.0
) -> None:
    """Send beginning-of-day briefing with positions, equity, and recent trades."""
    export_csv()
    recent = get_recent_trades(10)
    pnl_rows = get_daily_pnl_rows(7)

    lines = [
        "Morning Briefing",
        f"Strategy: {config.strategy.strategy_id} ({config.strategy.symbol})",
        f"Environment: {config.environment}",
        f"Account Equity: ${equity:,.2f}" if equity else "",
        "",
        "Open Positions:",
        positions_summary or "  None",
        "",
        "Recent Trades:",
    ]
    for t in recent[:5]:
        lines.append(
            f"  {t['timestamp'][:10]} {t['symbol']} {t['direction']} "
            f"qty={t['qty']} @ {t.get('entry_price', 'N/A')}"
        )

    if pnl_rows:
        lines.append("")
        lines.append("Recent Daily P&L:")
        for row in pnl_rows[:3]:
            lines.append(f"  {row['date']}: ${row['pnl']:,.2f}")

    body = "\n".join(line for line in lines if line is not None)
    _send_email(config, f"[Bot] Morning Briefing — {config.strategy.symbol}", body[:2000])


def send_eod_email(
    config: AppConfig, equity: float = 0.0, daily_pnl: float = 0.0
) -> None:
    """Send end-of-day performance summary."""
    export_csv()
    recent = get_recent_trades(20)

    lines = [
        "End of Day Report",
        f"Strategy: {config.strategy.strategy_id} ({config.strategy.symbol})",
        f"Environment: {config.environment}",
        f"Equity: ${equity:,.2f}" if equity else "",
        f"Today P&L: ${daily_pnl:,.2f}" if equity else "",
        f"Trades today: {len(recent)}",
        "",
    ]
    for t in recent[:5]:
        lines.append(
            f"  {t['symbol']} {t['direction']} qty={t['qty']} status={t['status']}"
        )

    body = "\n".join(lines)
    _send_email(config, f"[Bot] EOD Report — {config.strategy.symbol}", body[:2000])
