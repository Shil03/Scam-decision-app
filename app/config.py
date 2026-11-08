"""Rule configuration: thresholds risk owners can tune without a code release.

The file is re-read when it changes on disk. An invalid edit never replaces the last
good config: it is logged, and the store reports itself stale so /health shows degraded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, ValidationError, model_validator

from app.schemas import ScamCategory

log = logging.getLogger("app_shield.config")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Thresholds(_Frozen):
    new_payee_warn_amount: Decimal = Field(gt=0)
    new_payee_hold_amount: Decimal = Field(gt=0)
    high_value_amount: Decimal = Field(gt=0)
    payee_mismatch_hold_amount: Decimal = Field(gt=0)
    unusual_payment_multiplier: Decimal = Field(ge=1)
    unusual_payment_min_amount: Decimal = Field(gt=0)
    rapid_payee_additions_24h: StrictInt = Field(ge=1)
    soft_signal_escalation_count: StrictInt = Field(ge=2)

    @model_validator(mode="after")
    def _warn_below_hold(self) -> "Thresholds":
        if self.new_payee_warn_amount >= self.new_payee_hold_amount:
            raise ValueError("new_payee_warn_amount must be below new_payee_hold_amount")
        return self


class Dependencies(_Frozen):
    mule_list_enabled: StrictBool


Keyword = Annotated[str, StringConstraints(min_length=2, max_length=40)]


class RuleConfig(_Frozen):
    ruleset_version: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.\-]{1,32}$")]
    thresholds: Thresholds
    reference_keywords: dict[ScamCategory, list[Keyword]]
    dependencies: Dependencies


@dataclass(frozen=True)
class LoadedConfig:
    rules: RuleConfig
    config_hash: str


class ConfigError(Exception):
    """No valid configuration is available."""


def parse_config(raw: bytes) -> LoadedConfig:
    data = json.loads(raw, parse_float=Decimal)
    rules = RuleConfig.model_validate(data)
    return LoadedConfig(rules=rules, config_hash=hashlib.sha256(raw).hexdigest())


class ConfigStore:
    """Holds the active config and reloads it when the file changes."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._signature: tuple[int, int] | None = None
        self.last_error: str | None = None
        try:
            raw = self._path.read_bytes()
            self._signature = self._stat_signature()
            self._current = parse_config(raw)
        except (OSError, ValueError, ValidationError) as exc:
            raise ConfigError(f"cannot load rule config from {self._path}: {exc}") from exc

    @property
    def stale(self) -> bool:
        return self.last_error is not None

    def _stat_signature(self) -> tuple[int, int]:
        st = self._path.stat()
        return (st.st_mtime_ns, st.st_size)

    def get(self) -> LoadedConfig:
        try:
            signature = self._stat_signature()
        except OSError as exc:
            self._record_error(f"config file unreadable: {exc.__class__.__name__}")
            return self._current
        if signature != self._signature:
            with self._lock:
                if signature != self._signature:
                    self._signature = signature
                    self._reload()
        return self._current

    def _reload(self) -> None:
        try:
            candidate = parse_config(self._path.read_bytes())
        except (OSError, ValueError, ValidationError) as exc:
            self._record_error(f"invalid config edit ignored: {exc}")
            return
        if candidate.config_hash != self._current.config_hash:
            log.info("rule config reloaded: version=%s hash=%s",
                     candidate.rules.ruleset_version, candidate.config_hash[:12])
        self._current = candidate
        self.last_error = None

    def _record_error(self, message: str) -> None:
        self.last_error = message
        log.error("%s; keeping version=%s", message, self._current.rules.ruleset_version)
