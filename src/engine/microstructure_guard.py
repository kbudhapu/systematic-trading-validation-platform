"""
Real-time microstructure truth filter — halts, LULD, spread, and quote freshness.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from src.ingestor.level1_depth_cache import (
    Level1DepthQuote,
    get_level1_depth_cache,
)

log = structlog.get_logger()

DEFAULT_MAX_ALLOWED_SPREAD_PCT = 0.015
DEFAULT_MAX_QUOTE_STALE_SECONDS = 5.0
DEFAULT_FRESH_BAR_WINDOW_SECONDS = 120.0

DEFAULT_HALT_RECOVERY_CONFIRMATION_SECONDS = 60.0

HALT_STATUS_TOKENS = frozenset(
    {
        "H",
        "HALT",
        "HALTED",
        "SUSPENDED",
        "PAUSED",
        "T1",
        "T2",
        "T5",
        "T12",
    }
)
LULD_STATUS_TOKENS = frozenset(
    {
        "LULD",
        "LIMIT_UP",
        "LIMIT_DOWN",
        "LIMITUP",
        "LIMITDOWN",
        "LU",
        "LD",
    }
)
RESUME_STATUS_CODES = frozenset({"ACTIVE", "RESUME", "RESUMED", "NORMAL"})

# PR-2 (a) DETECTOR PRECISION, 2026-07-20. This set previously contained "M", "P" and "Q". Those
# are not halt indicators — under CTA/UTP they are routine trade conditions:
#     M = Market Center Official Close   Q = Market Center Official Open
#     P = Prior Reference Price
# M and Q are emitted on ordinary opening and closing prints, so a normal session could latch
# regulatory_halt on QQQ every day and never clear (there is no resume path on the quote/trade
# ingress — see the recovery work below). Live: on a soak process running continuously since Sat
# 07-18, the false latch appeared at 13:33:25Z on Monday 07-20, three minutes after the 13:30Z
# open, and suppressed a real short entry at 13:46:41Z.
#
# Only unambiguous halt tokens remain. Single letters that carry a non-halt meaning anywhere in
# CTA/UTP are excluded on purpose — a halt is rare and a false positive is expensive, so this set
# must stay narrow and explicit rather than convenient.
HALT_CONDITION_TOKENS = frozenset({"H", "HALT", "HALTED"})
LULD_CONDITION_TOKENS = frozenset({"L", "LU", "LD", "LULD"})

# Negations that must NOT set the latch. The old detector was a bare substring scan, so any
# message CONTAINING "HALTED" latched — including one saying trading was not halted.
_NEGATION_PREFIXES = (
    "NOT ",
    "NO ",
    "NEVER ",
    "UN",
    "NON-",
    "NON ",
)
_HALT_PHRASE_RE = re.compile(
    r"(?<![A-Z0-9])(?:TRADING\s+HALT(?:ED)?|HALTED|SUSPENDED|TRADING\s+SUSPENSION)(?![A-Z0-9])"
)

_LULD_BAND_RE = re.compile(
    r"(?:limit[\s_-]*(?:up|down)|luld|band)[^\d]*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_tokens(value: str | list[str] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        raw_parts = value
    else:
        raw_parts = str(value).replace(",", " ").split()
    return {part.strip().upper() for part in raw_parts if str(part).strip()}


def _token_hits(tokens: set[str], vocabulary: frozenset[str]) -> bool:
    return bool(tokens & vocabulary)


def _is_resume_status(
    status_code: str,
    status_message: str,
    reason_code: str,
    reason_message: str,
) -> bool:
    code = str(status_code or "").strip().upper()
    if code in RESUME_STATUS_CODES:
        return True
    text = f"{status_message} {reason_message}".upper()
    return any(
        phrase in text
        for phrase in (
            "TRADING RESUMED",
            "RESUME TRADING",
            "HALT CANCELLED",
            "HALT LIFTED",
        )
    )


def _is_halt_status(
    status_code: str,
    status_message: str,
    reason_code: str,
    reason_message: str,
) -> bool:
    """Detect a regulatory halt from a market-status message.

    PR-2 (a): STATUS-CODE FIRST. The authoritative signal is the code the venue publishes; free
    text is a fallback only. The previous implementation went straight to
    `any(term in text for term in ("HALTED", "SUSPENDED", "TRADING HALT"))` over the concatenation
    of all four fields, which latched on:
      - any substring occurrence, e.g. "TRADING HALTED" inside "NOT HALTED" or "UNHALTED";
      - words that merely contain a token as a fragment;
      - end-of-session or informational text mentioning a halt that had already resolved.
    A latch here has no timer-based escape, so a single false positive is permanent for the life
    of the process (the 07-13 → 07-20 QQQ incident).
    """
    code = str(status_code or "").strip().upper()
    if code in HALT_STATUS_TOKENS:
        return True
    reason = str(reason_code or "").strip().upper()
    if reason in HALT_STATUS_TOKENS:
        return True
    # Free-text fallback: word-boundary matched and negation-safe.
    for text in (status_message, reason_message):
        upper = str(text or "").upper()
        for match in _HALT_PHRASE_RE.finditer(upper):
            preceding = upper[: match.start()]
            tail = preceding.rstrip()
            negated = False
            for prefix in _NEGATION_PREFIXES:
                # "UN"/"NON-" bind directly to the token; the word forms need a separating space.
                if prefix in ("UN", "NON-"):
                    if preceding.endswith(prefix):
                        negated = True
                        break
                elif tail.endswith(prefix.strip()) and len(preceding) > len(tail):
                    negated = True
                    break
            if not negated:
                return True
    return False


def _halt_trigger_description(
    status_code: str,
    status_message: str,
    reason_code: str,
    reason_message: str,
) -> str:
    """Verbatim rendering of the message that set the latch, for the telemetry requirement."""
    return (
        f"status_code={status_code!r} status_message={status_message!r} "
        f"reason_code={reason_code!r} reason_message={reason_message!r}"
    )


def _is_luld_status(
    status_code: str,
    status_message: str,
    reason_code: str,
    reason_message: str,
) -> bool:
    tokens = _normalize_tokens(
        [status_code, status_message, reason_code, reason_message]
    )
    if _token_hits(tokens, LULD_STATUS_TOKENS):
        return True
    text = f"{status_code} {status_message} {reason_code} {reason_message}".upper()
    return any(term in text for term in LULD_STATUS_TOKENS)


@dataclass(frozen=True)
class MicrostructureGuardConfig:
    max_allowed_spread_pct: float = DEFAULT_MAX_ALLOWED_SPREAD_PCT
    max_quote_stale_seconds: float = DEFAULT_MAX_QUOTE_STALE_SECONDS
    fresh_bar_window_seconds: float = DEFAULT_FRESH_BAR_WINDOW_SECONDS
    halt_recovery_confirmation_seconds: float = DEFAULT_HALT_RECOVERY_CONFIRMATION_SECONDS


@dataclass
class SymbolMicrostructureState:
    symbol: str
    regulatory_halt: bool = False
    luld_active: bool = False
    luld_upper: float | None = None
    luld_lower: float | None = None
    market_status_code: str = ""
    market_status_message: str = ""
    last_status_timestamp: datetime | None = None
    last_quote_timestamp: datetime | None = None
    last_bar_timestamp: datetime | None = None
    last_trade_timestamp: datetime | None = None
    bid_price: float = 0.0
    ask_price: float = 0.0
    last_trade_price: float = 0.0
    spread_pct: float = 0.0
    quote_conditions: set[str] = field(default_factory=set)
    trade_conditions: set[str] = field(default_factory=set)
    # PR-2 (b) recovery bookkeeping. `halt_trigger` is the verbatim message/conditions that set
    # the latch; `halt_clear_candidate_since` is the start of the current unbroken run of
    # tradeable-looking quotes and is reset to None the moment that run is broken or becomes
    # unobservable. Recovery requires a sustained POSITIVE reading, never elapsed time.
    halt_latched_at: datetime | None = None
    halt_trigger: str = ""
    halt_clear_candidate_since: datetime | None = None


@dataclass(frozen=True)
class MicrostructureVerdict:
    symbol: str
    safe_for_entries: bool
    blocks_entries: bool
    halt_status: bool = False
    luld_active: bool = False
    realized_spread_pct: float = 0.0
    stale_quote_detected: bool = False
    quote_age_seconds: float | None = None
    reason: str = ""


class MicrostructureGuard:
    """In-memory microstructure latch driven by websocket quote/trade/status feeds."""

    def __init__(
        self,
        config: MicrostructureGuardConfig | None = None,
        *,
        depth_cache: Any | None = None,
    ) -> None:
        self._config = config or MicrostructureGuardConfig()
        self._depth_cache = depth_cache or get_level1_depth_cache()
        self._lock = threading.RLock()
        self._states: dict[str, SymbolMicrostructureState] = {}

    def note_stream_bar(self, symbol: str, *, bar_timestamp: datetime) -> None:
        symbol = symbol.upper()
        with self._lock:
            state = self._states.setdefault(symbol, SymbolMicrostructureState(symbol=symbol))
            state.last_bar_timestamp = _coerce_utc(bar_timestamp)

    def note_quote(
        self,
        symbol: str,
        *,
        bid_price: float,
        ask_price: float,
        timestamp: datetime,
        conditions: str | list[str] | None = None,
        tape: str | None = None,
    ) -> None:
        symbol = symbol.upper()
        quote_ts = _coerce_utc(timestamp)
        condition_tokens = _normalize_tokens(conditions)
        if tape:
            condition_tokens.add(str(tape).upper())
        mid = 0.0
        if bid_price > 0.0 and ask_price > 0.0:
            mid = (bid_price + ask_price) / 2.0
        spread_pct = 0.0
        if mid > 0.0:
            spread_pct = max(ask_price - bid_price, 0.0) / mid
        # PR-2: the tape is a VENUE identifier (A/B/C), not a trading condition. It was being
        # folded into the condition set and then matched against the halt/LULD vocabularies, so a
        # tape letter could set a safety latch on its own. Halt matching now uses conditions only.
        halt_tokens = _normalize_tokens(conditions)
        with self._lock:
            state = self._states.setdefault(symbol, SymbolMicrostructureState(symbol=symbol))
            state.last_quote_timestamp = quote_ts
            state.bid_price = max(float(bid_price), 0.0)
            state.ask_price = max(float(ask_price), 0.0)
            state.spread_pct = spread_pct
            state.quote_conditions = condition_tokens
            if _token_hits(halt_tokens, HALT_CONDITION_TOKENS):
                self._latch_halt(
                    state,
                    trigger=f"quote conditions={sorted(halt_tokens)}",
                    timestamp=quote_ts,
                )
            if _token_hits(halt_tokens, LULD_CONDITION_TOKENS):
                state.luld_active = True
            # PR-2 (b): track the unbroken run of tradeable-looking quotes that recovery needs.
            self._note_recovery_evidence(state, quote_ts, halt_tokens)

    def note_trade(
        self,
        symbol: str,
        *,
        price: float,
        timestamp: datetime,
        conditions: str | list[str] | None = None,
        tape: str | None = None,
    ) -> None:
        symbol = symbol.upper()
        trade_ts = _coerce_utc(timestamp)
        condition_tokens = _normalize_tokens(conditions)
        if tape:
            condition_tokens.add(str(tape).upper())
        halt_tokens = _normalize_tokens(conditions)  # PR-2: tape excluded, see note_quote
        with self._lock:
            state = self._states.setdefault(symbol, SymbolMicrostructureState(symbol=symbol))
            state.last_trade_timestamp = trade_ts
            state.last_trade_price = max(float(price), 0.0)
            state.trade_conditions = condition_tokens
            if _token_hits(halt_tokens, HALT_CONDITION_TOKENS):
                self._latch_halt(
                    state,
                    trigger=f"trade conditions={sorted(halt_tokens)}",
                    timestamp=trade_ts,
                )
            if _token_hits(halt_tokens, LULD_CONDITION_TOKENS):
                state.luld_active = True

    def note_trading_status(
        self,
        symbol: str,
        *,
        status_code: str,
        status_message: str,
        reason_code: str,
        reason_message: str,
        timestamp: datetime,
        tape: str | None = None,
        limit_up_price: float | None = None,
        limit_down_price: float | None = None,
    ) -> None:
        symbol = symbol.upper()
        with self._lock:
            state = self._states.setdefault(symbol, SymbolMicrostructureState(symbol=symbol))
            state.market_status_code = str(status_code or "")
            state.market_status_message = str(status_message or "")
            state.last_status_timestamp = _coerce_utc(timestamp)
            if limit_up_price is not None and limit_up_price > 0.0:
                state.luld_upper = float(limit_up_price)
            if limit_down_price is not None and limit_down_price > 0.0:
                state.luld_lower = float(limit_down_price)
            for text in (status_message, reason_message):
                for match in _LULD_BAND_RE.finditer(str(text or "")):
                    try:
                        parsed = float(match.group(1))
                    except (TypeError, ValueError):
                        continue
                    if state.luld_lower is None:
                        state.luld_lower = parsed
                    elif state.luld_upper is None:
                        state.luld_upper = parsed
            if _is_resume_status(status_code, status_message, reason_code, reason_message):
                self._clear_halt(
                    state,
                    reason="explicit_resume_status",
                    detail=_halt_trigger_description(
                        status_code, status_message, reason_code, reason_message
                    ),
                )
                state.luld_active = False
            elif _is_halt_status(status_code, status_message, reason_code, reason_message):
                self._latch_halt(
                    state,
                    trigger=_halt_trigger_description(
                        status_code, status_message, reason_code, reason_message
                    ),
                    timestamp=state.last_status_timestamp,
                )
            if _is_luld_status(status_code, status_message, reason_code, reason_message):
                state.luld_active = True

    # ── PR-2 (b): halt latch lifecycle ────────────────────────────────────────────────────────
    #
    # SAFETY-STATE-RECOVERY DOCTRINE, applied verbatim. This is the second time this defect class
    # has cost us a trading window: HARD_CRITICAL_DEGRADE latched for six days with healthy
    # sensors and no recovery path, and the regulatory_halt latch then did the same thing for a
    # week on QQQ. A latched safety state MUST define how it un-latches, and that definition must
    # be a positive re-verification of the condition that set it — not a timer, not a restart, not
    # an operator remembering.
    #
    # The three rules this implements:
    #   1. Clear only on POSITIVE evidence: an unbroken run of tradeable-looking quotes (two-sided,
    #      inside the spread limit, no halt/LULD conditions) lasting at least the confirmation
    #      window. Any bad or halt-flagged quote resets the run to zero.
    #   2. NEVER on elapsed time alone. Silence is not evidence — see rule 3.
    #   3. Stay latched when status is UNKNOWABLE. If quotes are stale or absent we cannot observe
    #      tradeability, so the run resets and the latch holds. The fail-safe direction is
    #      preserved: this can only ever be as permissive as a live, healthy, halt-free tape.
    #
    # An explicit venue resume message still clears immediately — that is the authoritative signal
    # and needs no corroboration.

    def _latch_halt(
        self,
        state: SymbolMicrostructureState,
        *,
        trigger: str,
        timestamp: datetime | None,
    ) -> None:
        already = state.regulatory_halt
        state.regulatory_halt = True
        state.halt_clear_candidate_since = None
        if already:
            return
        state.halt_latched_at = timestamp
        state.halt_trigger = trigger
        log.warning(
            "microstructure_guard: HALT_LATCH_SET",
            symbol=state.symbol,
            trigger=trigger,
            latched_at=timestamp.isoformat() if timestamp else None,
        )

    def _clear_halt(
        self,
        state: SymbolMicrostructureState,
        *,
        reason: str,
        detail: str = "",
    ) -> None:
        if not state.regulatory_halt:
            state.halt_clear_candidate_since = None
            return
        log.warning(
            "microstructure_guard: HALT_LATCH_CLEARED",
            symbol=state.symbol,
            recovery_reason=reason,
            detail=detail,
            set_by_trigger=state.halt_trigger,
            latched_at=state.halt_latched_at.isoformat() if state.halt_latched_at else None,
        )
        state.regulatory_halt = False
        state.halt_latched_at = None
        state.halt_trigger = ""
        state.halt_clear_candidate_since = None

    def _note_recovery_evidence(
        self,
        state: SymbolMicrostructureState,
        quote_ts: datetime,
        condition_tokens: set[str],
    ) -> None:
        """Extend or reset the unbroken run of tradeable-looking quotes."""
        if not state.regulatory_halt:
            state.halt_clear_candidate_since = None
            return
        tradeable = (
            state.bid_price > 0.0
            and state.ask_price > 0.0
            and state.spread_pct <= self._config.max_allowed_spread_pct
            and not _token_hits(condition_tokens, HALT_CONDITION_TOKENS)
            and not _token_hits(condition_tokens, LULD_CONDITION_TOKENS)
        )
        if not tradeable:
            state.halt_clear_candidate_since = None
            return
        if state.halt_clear_candidate_since is None:
            state.halt_clear_candidate_since = quote_ts

    def _maybe_recover_halt(
        self,
        state: SymbolMicrostructureState,
        reference: datetime,
        *,
        quote_is_stale: bool,
    ) -> None:
        if not state.regulatory_halt:
            return
        # Rule 3: unobservable ⇒ hold the latch and discard any partial run.
        if quote_is_stale or state.last_quote_timestamp is None:
            state.halt_clear_candidate_since = None
            return
        since = state.halt_clear_candidate_since
        if since is None:
            return
        confirmed_seconds = (reference - since).total_seconds()
        if confirmed_seconds < self._config.halt_recovery_confirmation_seconds:
            return
        self._clear_halt(
            state,
            reason="positive_reverification",
            detail=(
                f"tradeable quotes sustained {confirmed_seconds:.1f}s "
                f"(>= {self._config.halt_recovery_confirmation_seconds:.1f}s), "
                f"spread_pct={state.spread_pct:.6g}"
            ),
        )

    def evaluate(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
    ) -> MicrostructureVerdict:
        symbol = symbol.upper()
        reference = _coerce_utc(now or datetime.now(timezone.utc))
        with self._lock:
            state = self._states.setdefault(symbol, SymbolMicrostructureState(symbol=symbol))
            halt_status = bool(state.regulatory_halt)
            luld_active = bool(state.luld_active)
            spread_pct = float(state.spread_pct)
            quote_ts = state.last_quote_timestamp
            bar_ts = state.last_bar_timestamp
            bid_price = state.bid_price
            ask_price = state.ask_price
            luld_upper = state.luld_upper
            luld_lower = state.luld_lower
            last_trade_price = state.last_trade_price

        stream_quote: Level1DepthQuote | None = self._depth_cache.get(symbol)
        if stream_quote is not None:
            spread_pct = float(stream_quote.spread_pct)
            quote_ts = stream_quote.timestamp
            bid_price = float(stream_quote.bid_price)
            ask_price = float(stream_quote.ask_price)

        quote_age_seconds: float | None = None
        if quote_ts is not None:
            quote_age_seconds = max((reference - _coerce_utc(quote_ts)).total_seconds(), 0.0)

        stale_quote_detected = False
        if bar_ts is not None and quote_ts is not None and quote_age_seconds is not None:
            bar_age = max((reference - _coerce_utc(bar_ts)).total_seconds(), 0.0)
            if (
                bar_age <= self._config.fresh_bar_window_seconds
                and quote_age_seconds > self._config.max_quote_stale_seconds
            ):
                stale_quote_detected = True

        spread_exceeded = spread_pct > self._config.max_allowed_spread_pct

        # PR-2 (b): attempt recovery only now — staleness is the input rule 3 depends on, and it is
        # not known until here. halt_status is then RE-READ, so a latch cleared on this pass stops
        # blocking on this pass rather than one cycle later.
        with self._lock:
            self._maybe_recover_halt(
                state, reference, quote_is_stale=stale_quote_detected or spread_exceeded
            )
            halt_status = bool(state.regulatory_halt)

        luld_breach = False
        if luld_active and luld_upper is not None and luld_lower is not None:
            reference_price = 0.0
            if bid_price > 0.0 and ask_price > 0.0:
                reference_price = (bid_price + ask_price) / 2.0
            elif last_trade_price > 0.0:
                reference_price = last_trade_price
            if reference_price > 0.0:
                luld_breach = (
                    reference_price >= luld_upper or reference_price <= luld_lower
                )

        blocks_entries = (
            halt_status
            or spread_exceeded
            or stale_quote_detected
            or luld_active
            or luld_breach
        )
        reason = ""
        if halt_status:
            reason = "regulatory_halt"
        elif spread_exceeded:
            reason = "spread_exceeded"
        elif stale_quote_detected:
            reason = "stale_quote"
        elif luld_active or luld_breach:
            reason = "luld_band"

        verdict = MicrostructureVerdict(
            symbol=symbol,
            safe_for_entries=not blocks_entries,
            blocks_entries=blocks_entries,
            halt_status=halt_status,
            luld_active=luld_active or luld_breach,
            realized_spread_pct=spread_pct,
            stale_quote_detected=stale_quote_detected,
            quote_age_seconds=quote_age_seconds,
            reason=reason,
        )
        if blocks_entries:
            log.warning(
                "microstructure_guard: STATE_UNSAFE",
                symbol=symbol,
                halt_status=halt_status,
                luld_active=verdict.luld_active,
                realized_spread_pct=round(spread_pct, 6),
                stale_quote_detected=stale_quote_detected,
                quote_age_seconds=quote_age_seconds,
                max_allowed_spread_pct=self._config.max_allowed_spread_pct,
                max_quote_stale_seconds=self._config.max_quote_stale_seconds,
                reason=reason,
            )
        return verdict

    def blocks_entry_signals(self, symbol: str, *, now: datetime | None = None) -> bool:
        return self.evaluate(symbol, now=now).blocks_entries

    def telemetry_snapshot(self) -> dict[str, Any]:
        with self._lock:
            symbols = sorted(self._states.keys())
        unsafe_symbols: list[str] = []
        for symbol in symbols:
            if self.evaluate(symbol).blocks_entries:
                unsafe_symbols.append(symbol)
        return {
            "microstructure_unsafe_symbols": unsafe_symbols,
            "microstructure_tracked_symbols": symbols,
            "max_allowed_spread_pct": self._config.max_allowed_spread_pct,
            "max_quote_stale_seconds": self._config.max_quote_stale_seconds,
        }


_guard: MicrostructureGuard | None = None
_guard_lock = threading.Lock()


def get_microstructure_guard() -> MicrostructureGuard:
    global _guard
    with _guard_lock:
        if _guard is None:
            _guard = MicrostructureGuard()
        return _guard


def reset_microstructure_guard() -> None:
    global _guard
    with _guard_lock:
        _guard = None
