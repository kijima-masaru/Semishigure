from pathlib import Path

import pytest

from semishigure.scenario.model import load_scenario, parse_duration, parse_rate
from semishigure.secrets import SecretError, SecretStore


def test_parse_helpers():
    assert parse_duration("180s") == 180
    assert parse_duration("1.5m") == 90
    assert parse_duration("250ms") == 0.25
    assert parse_rate("0.5/s") == 0.5
    assert parse_rate("30/m") == 0.5


def test_example_scenario_loads():
    sc = load_scenario(Path(__file__).parent.parent / "examples" / "dev-freeswitch.yaml")
    assert sc.caller.auth_user == "9100"
    assert sc.caller.headers[0] == ("X-LANG", "en")
    assert sc.caller.headers[1] == ("X-ENABLE-TRANSCRIBE", "true")
    assert [e.user for e in sc.answerer.extensions] == ["9001", "9002", "9003", "9004"]
    assert sc.load.call_duration == 180
    assert sc.pbx.domain == "pbx.semishigure.test"


def test_secret_store_env_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SEMISHIGURE_SECRET_EXT", "from-env")
    store = SecretStore(home=tmp_path)
    assert store.resolve("secret:ext") == "from-env"
    monkeypatch.delenv("SEMISHIGURE_SECRET_EXT")
    store.set("ext", "from-file")
    assert (tmp_path / "secrets.enc").exists() and (tmp_path / "master.key").exists()
    assert b"from-file" not in (tmp_path / "secrets.enc").read_bytes()
    assert SecretStore(home=tmp_path).resolve("secret:ext") == "from-file"
    assert store.resolve("literal:abc") == "abc"
    with pytest.raises(SecretError):
        store.resolve("plaintext-password")
    with pytest.raises(SecretError):
        store.resolve("secret:missing")
