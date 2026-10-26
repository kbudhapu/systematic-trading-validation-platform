"""NYSE session calendar (E5/R4).

Canonical home for the market session calendar. Extracted verbatim from
`src/engine/slo_monitor.py`, which had accreted it and was imported for the
calendar by data-quality monitors, the heartbeat watchdog, and the SIP scheduler
(F7: one concept reachable through an operational-monitor module). `slo_monitor`
now re-exports these names for backward compatibility.

NOTE ON THE OTHER "session" concept: the PSD resampler / SIP session-boundary
logic uses a DISTINCT, gap-based notion of a session (inferred from inter-bar gaps
in a data series), not this holiday/early-close NYSE calendar. They are different
concepts with different inputs and are deliberately NOT merged here (documented in
the E5 log entry / architecture review F7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
RTH_CLOSE_REGULAR = time(16, 0)
RTH_CLOSE_EARLY = time(13, 0)

NYSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2025, 1, 1),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    }
)

NYSE_EARLY_CLOSE: frozenset[date] = frozenset(
    {
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2027, 11, 26),
        date(2027, 12, 31),
    }
)


@dataclass(frozen=True)
class SessionBounds:
    session_date: date
    open_et: datetime
    close_et: datetime
    session_minutes: float
    early_close: bool
    trading_day: bool


class MarketSessionCalendar:
    """NYSE session calendar with holiday and early-close awareness."""

    def __init__(
        self,
        holidays: frozenset[date] = NYSE_HOLIDAYS,
        early_closes: frozenset[date] = NYSE_EARLY_CLOSE,
    ) -> None:
        self._holidays = holidays
        self._early_closes = early_closes

    def is_trading_day(self, session_date: date) -> bool:
        if session_date.weekday() >= 5:
            return False
        return session_date not in self._holidays

    def is_early_close(self, session_date: date) -> bool:
        return session_date in self._early_closes

    def session_close_time(self, session_date: date) -> time:
        if self.is_early_close(session_date):
            return RTH_CLOSE_EARLY
        return RTH_CLOSE_REGULAR

    def session_bounds(self, session_date: date) -> SessionBounds:
        trading_day = self.is_trading_day(session_date)
        open_dt = datetime.combine(session_date, RTH_OPEN, tzinfo=ET)
        close_t = self.session_close_time(session_date)
        close_dt = datetime.combine(session_date, close_t, tzinfo=ET)
        session_minutes = (
            (close_dt - open_dt).total_seconds() / 60.0 if trading_day else 0.0
        )
        return SessionBounds(
            session_date=session_date,
            open_et=open_dt,
            close_et=close_dt,
            session_minutes=session_minutes,
            early_close=self.is_early_close(session_date),
            trading_day=trading_day,
        )

    def is_within_rth(self, timestamp: datetime) -> bool:
        et = timestamp.astimezone(ET)
        bounds = self.session_bounds(et.date())
        if not bounds.trading_day:
            return False
        t = et.time()
        return RTH_OPEN <= t <= bounds.close_et.time()

    def last_session_close(self, reference: datetime) -> datetime:
        et = reference.astimezone(ET)
        cursor = et.date()
        for _ in range(10):
            if self.is_trading_day(cursor):
                bounds = self.session_bounds(cursor)
                close_dt = bounds.close_et
                if et >= close_dt or et.date() > cursor:
                    return close_dt.astimezone(timezone.utc)
            cursor -= timedelta(days=1)
        return reference.astimezone(timezone.utc)

    def effective_reference_time(
        self,
        exchange_reference: datetime,
        *,
        asset_class: str = "stock",
    ) -> datetime:
        if asset_class == "crypto":
            return exchange_reference.astimezone(timezone.utc)
        if self.is_within_rth(exchange_reference):
            return exchange_reference.astimezone(timezone.utc)
        return self.last_session_close(exchange_reference)

    def expected_bars_for_session(
        self,
        session_date: date,
        bar_minutes: int,
    ) -> int:
        bounds = self.session_bounds(session_date)
        if not bounds.trading_day or bar_minutes <= 0:
            return 0
        return max(1, int(bounds.session_minutes // bar_minutes))
