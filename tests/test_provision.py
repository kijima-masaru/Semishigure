"""PBX provisioning against temporary configuration trees (no PBX: reload is a no-op)."""

from pathlib import Path

import pytest

from semishigure.pbx.executor import LocalExecutor
from semishigure.pbx.profile import PbxProfile
from semishigure.pbx.provision import Provisioner, ProvisionPlan, ProvisionStore, flavor_of
from semishigure.secrets import SecretStore

VANILLA_DIR = """<include>
  <domain name="$${domain}">
    <groups>
      <group name="default">
        <users>
          <user id="1000"><params><param name="password" value="x"/></params></user>
        </users>
      </group>
    </groups>
  </domain>
</include>
"""
VANILLA_DP = """<include>
  <context name="default">
    <extension name="unloop"><condition field="destination_number" expression="^0$"><action application="hangup"/></condition></extension>
  </context>
</include>
"""


@pytest.mark.asyncio
async def test_freeswitch_files_and_restore(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEMISHIGURE_HOME", str(tmp_path / "home"))
    conf = tmp_path / "fs"
    (conf / "directory").mkdir(parents=True)
    (conf / "dialplan").mkdir()
    (conf / "directory" / "default.xml").write_text(VANILLA_DIR)
    (conf / "dialplan" / "default.xml").write_text(VANILLA_DP)
    profile = PbxProfile(name="p", type="freeswitch", conf_dir=str(conf), fs_cli="true", domain="d.test")
    secrets = SecretStore(tmp_path / "home")
    prov = Provisioner(profile, LocalExecutor(), secrets, None)
    plan = ProvisionPlan(caller="9100", answerers=["9001", "9002"], ring_group="8001", group_limit=7, secret_prefix="lt")
    rec = await prov.apply(plan)
    users = (conf / "directory" / "default" / "semishigure-loadtest.xml").read_text()
    dp = (conf / "dialplan" / "default" / "semishigure-loadtest.xml").read_text()
    assert '<user id="9100">' in users and '<user id="9002">' in users
    pw = secrets.resolve("secret:lt_9001")
    assert len(pw) == 16 and pw in users and pw not in str(rec)
    assert 'expression="^8001$"' in dp and "user/9001@${domain_name},user/9002@${domain_name}" in dp and "hash semishigure semishigure-loadtest 7" in dp
    # include lines were added to the vanilla files (and backed up)
    assert 'data="default/*.xml"' in (conf / "directory" / "default.xml").read_text()
    assert 'data="default/*.xml"' in (conf / "dialplan" / "default.xml").read_text()
    assert {Path(m["path"]) for m in rec["modified_files"]} == {conf / "directory" / "default.xml", conf / "dialplan" / "default.xml"}  # paths are POSIX-joined (remote PBX host)
    assert rec["secret_names"] == ["lt_9100", "lt_9001", "lt_9002"]
    store = ProvisionStore(tmp_path / "home" / "provision.yaml")
    store.put("p", rec)
    assert "lt_9001" in store.get("p")["secret_names"] and pw not in (tmp_path / "home" / "provision.yaml").read_text(encoding="utf-8")
    res = await prov.remove(store.get("p"))
    assert not (conf / "directory" / "default" / "semishigure-loadtest.xml").exists()
    assert (conf / "directory" / "default.xml").read_text() == VANILLA_DIR and (conf / "dialplan" / "default.xml").read_text() == VANILLA_DP
    assert any("restored" in n for n in res["notes"])


@pytest.mark.asyncio
async def test_asterisk_files_and_restore(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEMISHIGURE_HOME", str(tmp_path / "home"))
    conf = tmp_path / "ast"
    conf.mkdir()
    (conf / "pjsip.conf").write_text("[transport-udp]\ntype=transport\nprotocol=udp\nbind=0.0.0.0\n")
    (conf / "extensions.conf").write_text("[general]\nstatic=yes\n\n[default]\nexten => 100,1,Hangup()\n")
    profile = PbxProfile(name="a", type="asterisk", conf_dir=str(conf), fs_cli="true")
    secrets = SecretStore(tmp_path / "home")
    prov = Provisioner(profile, LocalExecutor(), secrets, None)
    rec = await prov.apply(ProvisionPlan(caller="9100", answerers=["9001", "9002"], ring_group="8001", group_limit=0))
    pj = (conf / "semishigure-loadtest-pjsip.conf").read_text()
    dp = (conf / "semishigure-loadtest-extensions.conf").read_text()
    assert "[9001](semishigure-loadtest-endpoint)" in pj and "username=9001" in pj and secrets.resolve("secret:ext_9001") in pj
    assert "exten => 8001,1" in dp and "Dial(PJSIP/9001&PJSIP/9002,30,b(semishigure^predial^1))" in dp and "GROUP_COUNT" not in dp
    assert "#include semishigure-loadtest-pjsip.conf" in (conf / "pjsip.conf").read_text()
    assert "#include semishigure-loadtest-extensions.conf" in (conf / "extensions.conf").read_text()
    await prov.remove(rec)
    assert (conf / "pjsip.conf").read_text() == "[transport-udp]\ntype=transport\nprotocol=udp\nbind=0.0.0.0\n"
    assert not (conf / "semishigure-loadtest-pjsip.conf").exists()


def test_flavor_and_defaults():
    assert flavor_of(PbxProfile(name="x", type="freeswitch", extra={"flavor": "fusionpbx"})) == "fusionpbx"
    assert flavor_of(PbxProfile(name="x", type="asterisk")) == "asterisk"
    plan = ProvisionPlan(caller="9100", answerers=["9100", "9001"])
    assert plan.extensions == ["9100", "9001"] and plan.secret_name("9001") == "ext_9001"
