import json
import os
from pathlib import Path

import pytest

from app.config import ConfigError, ConfigStore
from app.mule_list import MuleList, MuleResult
from tests.conftest import DEFAULT_MULE_LIST, write_json


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_shipped_config_is_valid(config_file):
    loaded = ConfigStore(config_file).get()
    assert loaded.rules.ruleset_version
    assert len(loaded.config_hash) == 64


def test_threshold_change_is_picked_up_without_restart(config_file):
    store = ConfigStore(config_file)
    data = json.loads(config_file.read_text())
    data["thresholds"]["new_payee_warn_amount"] = "200.00"
    data["ruleset_version"] = "2026.11.2"
    write_json(config_file, data)
    _bump_mtime(config_file)
    loaded = store.get()
    assert str(loaded.rules.thresholds.new_payee_warn_amount) == "200.00"
    assert loaded.rules.ruleset_version == "2026.11.2"
    assert not store.stale


@pytest.mark.parametrize("bad_edit", [
    lambda d: d["thresholds"].update(new_payee_warn_amount="-1"),
    lambda d: d["thresholds"].update(new_payee_warn_amount="20000.00"),   # above hold amount
    lambda d: d["thresholds"].update(soft_signal_escalation_count=0),
    lambda d: d.update(unknown_key=True),
    lambda d: d["dependencies"].update(mule_list_enabled="no"),
])
def test_invalid_edit_keeps_last_good_config_and_reports_stale(config_file, bad_edit):
    store = ConfigStore(config_file)
    good = store.get()
    data = json.loads(config_file.read_text())
    bad_edit(data)
    write_json(config_file, data)
    _bump_mtime(config_file)
    assert store.get() == good
    assert store.stale


def test_unparseable_edit_keeps_last_good_and_recovers(config_file):
    store = ConfigStore(config_file)
    good = store.get()
    original = config_file.read_text()
    config_file.write_text("{ not json", encoding="utf-8")
    _bump_mtime(config_file)
    assert store.get() == good and store.stale
    config_file.write_text(original, encoding="utf-8")
    _bump_mtime(config_file)
    store.get()
    assert not store.stale


def test_no_valid_config_at_startup_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError):
        ConfigStore(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        ConfigStore(bad)


def test_mule_list_match_and_no_match():
    mules = MuleList(DEFAULT_MULE_LIST)
    assert mules.check("999-001", "11110001", enabled=True) is MuleResult.MATCH
    assert mules.check("999001", "11110001", enabled=True) is MuleResult.MATCH
    assert mules.check("062-000", "12345678", enabled=True) is MuleResult.NO_MATCH
    assert mules.status(enabled=True) == "up"


def test_mule_list_switched_off_is_unavailable():
    mules = MuleList(DEFAULT_MULE_LIST)
    assert mules.check("999-001", "11110001", enabled=False) is MuleResult.UNAVAILABLE
    assert mules.status(enabled=False) == "disabled"


def test_mule_list_missing_or_corrupt_is_unavailable(tmp_path):
    assert MuleList(tmp_path / "nope.json").check("062-000", "12345678", enabled=True) is MuleResult.UNAVAILABLE
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text('{"accounts": [{"bsb": "1"}]}', encoding="utf-8")
    mules = MuleList(corrupt)
    assert mules.check("062-000", "12345678", enabled=True) is MuleResult.UNAVAILABLE
    assert mules.status(enabled=True) == "down"
