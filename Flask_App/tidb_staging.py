"""Explicit TiDB-staging connections; never fall back to production settings.

This foundation module is deliberately not imported by the production database
configuration yet. Importing it performs no network requests or schema changes.
Credentials are read only from TIDB_STAGING_* environment variables, not .env.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import ssl
from typing import Mapping

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL


SCHEMAS = {
    "mlb": "ticketsignal_staging_mlb",
    "nfl": "ticketsignal_staging_nfl",
    "nhl": "ticketsignal_staging_nhl",
}
# A DNS label boundary is required: neither an unrelated host nor a suffix
# lookalike such as tidbcloud.com.attacker.example is accepted.
_TIDB_HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+tidbcloud\.com\Z"
)


@dataclass(frozen=True)
class StagingTiDBConfig:
    host: str
    username: str
    password: str = field(repr=False)
    ca_file: str | None = None

    def __post_init__(self) -> None:
        if not _TIDB_HOST.fullmatch(self.host) or len(self.host) > 253:
            raise ValueError("Staging requires a valid *.tidbcloud.com hostname.")
        if not self.username.strip():
            raise ValueError("TIDB_STAGING_USERNAME is required.")
        if not self.password.strip():
            raise ValueError("TIDB_STAGING_PASSWORD is required.")
        if self.ca_file is not None and not Path(self.ca_file).is_file():
            raise ValueError("TIDB_STAGING_CA_FILE must name an existing CA bundle.")

    @classmethod
    def from_environment(
        cls, values: Mapping[str, str] | None = None
    ) -> StagingTiDBConfig:
        """Read isolated settings; MYSQL_* and production .env are ignored."""
        env = os.environ if values is None else values
        required = ("TIDB_STAGING_HOST", "TIDB_STAGING_USERNAME", "TIDB_STAGING_PASSWORD")
        missing = [key for key in required if not env.get(key, "").strip()]
        if missing:
            raise ValueError("Missing staging settings: " + ", ".join(missing))
        ca_file = env.get("TIDB_STAGING_CA_FILE", "").strip() or None
        return cls(
            host=env["TIDB_STAGING_HOST"].strip().lower(),
            username=env["TIDB_STAGING_USERNAME"].strip(),
            password=env["TIDB_STAGING_PASSWORD"],
            ca_file=ca_file,
        )

    def url(self, sport: str) -> URL:
        """Only the three staging schemas are allowed; never sys or production."""
        database = SCHEMAS.get(sport)
        if database is None:
            raise ValueError("Staging sport must be mlb, nfl, or nhl.")
        return URL.create(
            "mysql+pymysql",
            username=self.username,
            password=self.password,
            host=self.host,
            port=4000,
            database=database,
            query={"charset": "utf8mb4"},
        )

    def tls_context(self) -> ssl.SSLContext:
        """Require certificate AND hostname validation using trusted roots."""
        context = ssl.create_default_context(cafile=self.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        return context


def create_staging_engine(
    sport: str, *, config: StagingTiDBConfig | None = None
) -> Engine:
    """Build a lazy engine; caller owns disposal and any explicit SQL work.

    This never creates schemas or tables. Callers must not log a connection URL
    with its password exposed or log raw driver exceptions containing SQL data.
    """
    config = config if config is not None else StagingTiDBConfig.from_environment()
    return create_engine(
        config.url(sport),
        pool_pre_ping=True,
        pool_recycle=240,
        pool_size=1,
        max_overflow=0,
        pool_timeout=20,
        echo=False,
        hide_parameters=True,
        connect_args={
            "ssl": config.tls_context(),
            "connect_timeout": 10,
            "read_timeout": 90,
            "write_timeout": 90,
        },
    )
