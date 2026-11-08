"""SQLite persistence. One short-lived connection per operation; WAL for concurrent readers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id         TEXT PRIMARY KEY,
    payment_id          TEXT NOT NULL UNIQUE,
    correlation_id      TEXT NOT NULL UNIQUE,
    customer_id         TEXT NOT NULL,
    decision            TEXT NOT NULL CHECK (decision IN ('ALLOW','WARN','HOLD')),
    reason_codes        TEXT NOT NULL,          -- JSON array
    scam_category       TEXT,
    ruleset_version     TEXT NOT NULL,
    config_hash         TEXT NOT NULL,
    amount              TEXT NOT NULL,          -- Decimal as string
    currency            TEXT NOT NULL,
    payee_token         TEXT NOT NULL,          -- HMAC of BSB+account, never the account
    payee_masked        TEXT NOT NULL,
    signals             TEXT NOT NULL,          -- JSON: derived, non-sensitive inputs used
    request_fingerprint TEXT NOT NULL,          -- HMAC of the validated request (idempotency)
    response            TEXT NOT NULL,          -- JSON response as returned (for replay)
    degraded            INTEGER NOT NULL,
    latency_ms          REAL NOT NULL,
    created_at          TEXT NOT NULL           -- UTC ISO-8601
);

CREATE TABLE IF NOT EXISTS review_queue (
    review_id       TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL UNIQUE REFERENCES decisions(decision_id),
    payment_id      TEXT NOT NULL UNIQUE,
    customer_id     TEXT NOT NULL,
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    payee_masked    TEXT NOT NULL,
    payee_token     TEXT NOT NULL,
    reason_codes    TEXT NOT NULL,
    scam_category   TEXT,
    ruleset_version TEXT NOT NULL,
    priority        INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_review_status ON review_queue(status, priority, created_at);

CREATE TABLE IF NOT EXISTS rejections (
    rejection_id   TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    payment_id     TEXT,                        -- only if it was itself well formed
    error_fields   TEXT NOT NULL,               -- JSON: field paths and messages, never values
    created_at     TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1 FROM decisions LIMIT 1")
            return True
        except sqlite3.Error:
            return False
