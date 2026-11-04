"""tests/test_config_loader.py — Verify config.json is loaded correctly."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config_loader


def test_server_defaults():
    s = config_loader.server()
    assert "port" in s
    assert isinstance(s["port"], int)
    assert s["port"] > 0


def test_monitoring_interval_in_range():
    m = config_loader.monitoring()
    assert m["minIntervalSeconds"] <= m["pollIntervalSeconds"] <= m["maxIntervalSeconds"]


def test_mq_has_targets():
    mq = config_loader.mq()
    assert isinstance(mq.get("targets"), list)
    assert len(mq["targets"]) > 0


def test_active_target_resolvable():
    t = config_loader.mq_active_target()
    assert isinstance(t, dict)


def test_runbooks_are_strings():
    rb = config_loader.runbooks()
    for code, url in rb.items():
        assert isinstance(code, str)
        assert url.startswith("http")


def test_reload_returns_dict():
    result = config_loader.reload()
    assert isinstance(result, dict)
