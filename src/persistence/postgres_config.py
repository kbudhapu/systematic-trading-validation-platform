"""
Supabase PostgreSQL connection settings for direct and transaction-pool ports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus


@dataclass(frozen=True)
class PostgresConnectionSettings:
    host: str
    database: str
    user: str
    password: str
    direct_port: int
    pool_port: int
    sslmode: str

    @classmethod
    def from_env(cls) -> PostgresConnectionSettings | None:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if database_url:
            return cls.from_database_url(database_url)

        host = os.getenv("SUPABASE_DB_HOST", "").strip()
        user = os.getenv("SUPABASE_DB_USER", "").strip()
        password = os.getenv("SUPABASE_DB_PASSWORD", "").strip()
        if not host or not user or not password:
            return None

        return cls(
            host=host,
            database=os.getenv("SUPABASE_DB_NAME", "postgres").strip() or "postgres",
            user=user,
            password=password,
            direct_port=int(os.getenv("SUPABASE_DB_DIRECT_PORT", "5432")),
            pool_port=int(os.getenv("SUPABASE_DB_POOL_PORT", "6543")),
            sslmode=os.getenv("SUPABASE_DB_SSLMODE", "require").strip() or "require",
        )

    @classmethod
    def from_database_url(cls, database_url: str) -> PostgresConnectionSettings | None:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(database_url)
            if not parsed.hostname or not parsed.username or parsed.password is None:
                return None
            port = parsed.port or int(os.getenv("SUPABASE_DB_POOL_PORT", "6543"))
            database = (parsed.path or "/postgres").lstrip("/") or "postgres"
            return cls(
                host=parsed.hostname,
                database=database,
                user=parsed.username,
                password=parsed.password,
                direct_port=int(os.getenv("SUPABASE_DB_DIRECT_PORT", "5432")),
                pool_port=port,
                sslmode=os.getenv("SUPABASE_DB_SSLMODE", "require").strip() or "require",
            )
        except (TypeError, ValueError):
            return None

    def dsn(self, *, use_pool: bool = True) -> str:
        port = self.pool_port if use_pool else self.direct_port
        user = quote_plus(self.user)
        password = quote_plus(self.password)
        return (
            f"postgresql://{user}:{password}@{self.host}:{port}/{self.database}"
            f"?sslmode={quote_plus(self.sslmode)}"
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.database)
