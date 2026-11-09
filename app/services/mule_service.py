"""Local stub of the upstream mule account list.

It can be switched off in config to simulate an outage. When switched off, missing or
unreadable, every lookup returns UNAVAILABLE and the engine fails closed (HOLD).
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path

log = logging.getLogger("app_shield.mule_list")


class MuleResult(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    UNAVAILABLE = "UNAVAILABLE"


def account_key(bsb: str, account_number: str) -> str:
    return f"{bsb.replace('-', '')}:{account_number}"


class MuleService:
    """Mule-list dependency. Path is configurable; the enabled switch lives in config/rules.json."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._signature: tuple[int, int] | None = None
        self._keys: frozenset[str] = frozenset()

    def _load(self) -> frozenset[str] | None:
        try:
            st = self._path.stat()
            signature = (st.st_mtime_ns, st.st_size)
            if signature != self._signature:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._keys = frozenset(account_key(a["bsb"], a["account_number"]) for a in data["accounts"])
                self._signature = signature
            return self._keys
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._signature = None
            log.error("mule list unavailable: %s", exc.__class__.__name__)
            return None

    def _usable(self, min_entries: int) -> frozenset[str] | None:
        """The loaded list, or None if it is missing, unreadable or implausibly small.

        An empty or truncated list must not silently mean "no mules": that would ALLOW payments
        to known mule accounts (assessment finding D2). Below the minimum, the list is unavailable.
        """
        keys = self._load()
        if keys is not None and len(keys) < min_entries:
            log.error("mule list unavailable: %d entries, minimum is %d", len(keys), min_entries)
            return None
        return keys

    def check(self, bsb: str, account_number: str, enabled: bool, min_entries: int = 1) -> MuleResult:
        if not enabled:
            return MuleResult.UNAVAILABLE
        keys = self._usable(min_entries)
        if keys is None:
            return MuleResult.UNAVAILABLE
        return MuleResult.MATCH if account_key(bsb, account_number) in keys else MuleResult.NO_MATCH

    def status(self, enabled: bool, min_entries: int = 1) -> str:
        """For /health: 'up', 'disabled' or 'down' (missing, unreadable or below the minimum size)."""
        if not enabled:
            return "disabled"
        return "up" if self._usable(min_entries) is not None else "down"
