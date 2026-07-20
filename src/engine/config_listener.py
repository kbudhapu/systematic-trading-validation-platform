"""
PostgreSQL LISTEN/NOTIFY push plane for strategy configuration changes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.persistence.postgres_config import PostgresConnectionSettings

log = structlog.get_logger()

LISTEN_CHANNEL = "strategy_config_update"
RECONNECT_DELAY_SECONDS = 2.0
WAIT_TIMEOUT_SECONDS = 1.0
LIVENESS_PROBE_IDLE_SECONDS = 60.0
TCP_KEEPALIVES_IDLE = 30
TCP_KEEPALIVES_INTERVAL = 10
TCP_KEEPALIVES_COUNT = 3


class LocalConfigDirtyFlag:
    """Thread-safe in-memory dirty token for config push notifications."""

    __slots__ = ("_dirty", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dirty = False

    @property
    def is_set(self) -> bool:
        with self._lock:
            return self._dirty

    def set(self) -> None:
        with self._lock:
            self._dirty = True

    def clear(self) -> None:
        with self._lock:
            self._dirty = False


@dataclass
class SupabaseConfigListener:
    """
    Dedicated background thread holding a direct Postgres session subscribed to
    strategy configuration NOTIFY events.
    """

    postgres_settings: PostgresConnectionSettings | None = field(
        default_factory=PostgresConnectionSettings.from_env
    )
    listen_channel: str = LISTEN_CHANNEL
    reconnect_delay_seconds: float = RECONNECT_DELAY_SECONDS
    wait_timeout_seconds: float = WAIT_TIMEOUT_SECONDS
    liveness_probe_idle_seconds: float = LIVENESS_PROBE_IDLE_SECONDS
    _dirty_flag: LocalConfigDirtyFlag = field(
        default_factory=LocalConfigDirtyFlag,
        init=False,
        repr=False,
    )
    _stop_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _connection_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _active_connection: Any = field(default=None, init=False, repr=False)

    @property
    def local_config_dirty(self) -> bool:
        return self._dirty_flag.is_set

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def clear_local_config_dirty(self) -> None:
        self._dirty_flag.clear()

    def mark_local_config_dirty(self) -> None:
        self._dirty_flag.set()

    def start(self) -> None:
        if self.is_running:
            return
        if self.postgres_settings is None:
            log.info("config_listener_disabled", reason="postgres_not_configured")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._listen_loop,
            name="supabase-config-listener",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "config_listener_started",
            channel=self.listen_channel,
            direct_port=self.postgres_settings.direct_port,
        )

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        if not self.is_running:
            return
        self._stop_event.set()
        self._close_active_connection()
        assert self._thread is not None
        self._thread.join(timeout=max(timeout_seconds, 0.1))
        log.info("config_listener_stopped")

    def _close_active_connection(self) -> None:
        with self._connection_lock:
            if self._active_connection is not None:
                try:
                    self._active_connection.close()
                except Exception:
                    pass
                self._active_connection = None

    def _open_listen_connection(self):
        import psycopg

        if self.postgres_settings is None:
            raise RuntimeError("postgres not configured")
        conn = psycopg.connect(
            self.postgres_settings.dsn(use_pool=False),
            autocommit=True,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=TCP_KEEPALIVES_IDLE,
            keepalives_interval=TCP_KEEPALIVES_INTERVAL,
            keepalives_count=TCP_KEEPALIVES_COUNT,
        )
        with self._connection_lock:
            self._active_connection = conn
        return conn

    def _listen_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._consume_notifications()
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                log.warning(
                    "config_listener_connection_error",
                    error=str(exc),
                    channel=self.listen_channel,
                )
                self._close_active_connection()
                time.sleep(max(self.reconnect_delay_seconds, 0.5))

    def _probe_connection(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
        if row is None or int(row[0]) != 1:
            raise RuntimeError("config_listener_liveness_probe_failed")

    def _consume_notifications(self) -> None:
        conn = self._open_listen_connection()
        try:
            from psycopg import sql

            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("LISTEN {}").format(sql.Identifier(self.listen_channel))
                )
            log.info("config_listener_subscribed", channel=self.listen_channel)
            idle_elapsed = 0.0
            while not self._stop_event.is_set():
                ready = conn.wait(timeout=self.wait_timeout_seconds)
                if ready:
                    idle_elapsed = 0.0
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        self._handle_notify(notify)
                    continue
                idle_elapsed += self.wait_timeout_seconds
                if idle_elapsed < self.liveness_probe_idle_seconds:
                    continue
                self._probe_connection(conn)
                idle_elapsed = 0.0
        finally:
            self._close_active_connection()

    def _handle_notify(self, notify: Any) -> None:
        payload = str(getattr(notify, "payload", "") or "")
        channel = str(getattr(notify, "channel", "") or self.listen_channel)
        self._dirty_flag.set()
        log.info(
            "config_listener_notify_received",
            channel=channel,
            payload=payload[:512],
        )


_listener: SupabaseConfigListener | None = None
_listener_lock = threading.Lock()


def get_config_listener() -> SupabaseConfigListener:
    global _listener
    with _listener_lock:
        if _listener is None:
            _listener = SupabaseConfigListener()
        return _listener


def start_config_listener() -> SupabaseConfigListener:
    listener = get_config_listener()
    listener.start()
    return listener


def stop_config_listener() -> None:
    global _listener
    with _listener_lock:
        if _listener is not None:
            _listener.stop()
            _listener = None
