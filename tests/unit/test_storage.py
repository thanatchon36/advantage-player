from __future__ import annotations

from advantage_player.storage.db import make_engine, record_config_version


def test_config_version_dedup() -> None:
    e = make_engine("sqlite:///:memory:")
    assert record_config_version(e, "abc", {"x": 1}, "startup")
    assert not record_config_version(e, "abc", {"x": 1}, "startup")
    assert record_config_version(e, "def", {"x": 2}, "edit")
