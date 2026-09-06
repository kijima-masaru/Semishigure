"""Create the extensions and the ring group a load test needs on the target PBX
("プロビジョニング", README「PBX 側の設定をアプリから作る」).

Three flavours, chosen from the PBX profile:

* ``freeswitch``: directory users and a dialplan file dropped into
  ``<conf_dir>/directory/<dir>/`` and ``<conf_dir>/dialplan/<context>/`` (the
  vanilla configuration already includes ``*.xml`` there; the include line is
  added when missing), then ``reloadxml``.
* ``asterisk``: ``semishigure-pjsip.conf`` and ``semishigure-extensions.conf``
  in ``<conf_dir>`` with ``#include`` lines appended to ``pjsip.conf`` and
  ``extensions.conf``, then ``pjsip reload`` / ``dialplan reload``.
* ``fusionpbx`` (profile ``extra.flavor: fusionpbx``): rows in ``v_extensions``,
  ``v_ring_groups``, ``v_ring_group_destinations`` and ``v_dialplans`` through
  ``psql`` on the PBX host (credentials from ``/etc/fusionpbx/config.conf`` or a
  ``secret:`` reference), the file cache cleared, then ``reloadxml``.

Passwords are generated here, written into the PBX's own configuration (the PBX
has to know them) and stored in the encrypted secret store under
``<secret_prefix>_<extension>``; they never appear in YAML, in API responses or
in the provisioning record. Every file that is changed is backed up first and
``remove()`` puts everything back.
"""

from __future__ import annotations

import re
import secrets as pysecrets
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from semishigure.pbx.executor import Executor
from semishigure.pbx.profile import DEFAULT_DIR, PbxProfile
from semishigure.secrets import SecretStore

MARK = "semishigure-loadtest"
RING_GROUP_APP_UUID = "1d61fb65-1eec-bc73-a6ee-a6203b4fe6f2"  # FusionPBX ring_groups app


class ProvisionError(RuntimeError):
    pass


@dataclass
class ProvisionPlan:
    caller: str = "9100"
    answerers: list[str] = field(default_factory=lambda: ["9001", "9002", "9003", "9004"])
    ring_group: str = "8001"
    max_calls: int = 5  # per answerer extension (FusionPBX limit_max)
    group_limit: int = 20  # concurrent calls through the ring group (0 = none)
    secret_prefix: str = "ext"  # passwords go to secret:<prefix>_<extension>
    domain: str = ""  # SIP domain / FusionPBX tenant (default: profile.domain)
    context: str = "default"  # FreeSWITCH dialplan context; Asterisk uses its own "semishigure" context
    directory: str = "default"  # FreeSWITCH directory folder (directory/<name>.xml + <name>/)
    db_password_ref: str = ""  # FusionPBX: secret:NAME for the database (else /etc/fusionpbx/config.conf)

    @property
    def extensions(self) -> list[str]:
        return [self.caller] + [a for a in self.answerers if a != self.caller]

    def secret_name(self, ext: str) -> str:
        return f"{self.secret_prefix}_{ext}"


def _password() -> str:
    # 16 characters, letters and digits only: safe in XML, ini files and SQL
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(pysecrets.choice(alphabet) for _ in range(16))


def _q(s: str) -> str:
    return shlex.quote(s)


def _sql_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


class ProvisionStore:
    """``$SEMISHIGURE_HOME/provision.yaml``: one record per PBX profile."""

    def __init__(self, path: Path | None = None):
        self.path = path or (DEFAULT_DIR / "provision.yaml")

    def load_all(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return dict(data.get("provisioned") or {})

    def get(self, profile: str) -> dict | None:
        return self.load_all().get(profile)

    def put(self, profile: str, record: dict) -> None:
        all_ = self.load_all()
        all_[profile] = record
        self._save(all_)

    def delete(self, profile: str) -> bool:
        all_ = self.load_all()
        if profile not in all_:
            return False
        del all_[profile]
        self._save(all_)
        return True

    def _save(self, all_: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump({"provisioned": all_}, allow_unicode=True, sort_keys=False), encoding="utf-8")


def flavor_of(profile: PbxProfile) -> str:
    fl = str((profile.extra or {}).get("flavor") or "").lower()
    if fl == "fusionpbx":
        return "fusionpbx"
    return profile.type.lower()


class Provisioner:
    def __init__(self, profile: PbxProfile, executor: Executor, secrets: SecretStore, adapter=None):
        self.profile = profile
        self.ex = executor
        self.secrets = secrets
        self.adapter = adapter
        self.flavor = flavor_of(profile)

    # -- shell helpers ------------------------------------------------------------

    async def _run(self, cmd: str, timeout: float = 20.0, what: str = ""):
        r = await self.ex.run(cmd, timeout=timeout)
        if not r.ok:
            raise ProvisionError(f"{what or cmd.split()[0]}: {(r.stderr or r.stdout).strip()[:400]}")
        return r

    async def _read(self, path: str) -> str:
        r = await self.ex.run(f"cat {_q(path)}", timeout=10)
        if not r.ok:
            raise ProvisionError(f"cannot read {path}: {r.stderr.strip()[:200]}")
        return r.stdout

    async def _write(self, path: str, content: str) -> None:
        marker = "SEMISHIGURE_EOF_7f3a"
        await self._run(f"mkdir -p {_q(str(Path(path).parent))} && cat > {_q(path)} <<'{marker}'\n{content}\n{marker}\n", what=f"write {path}")

    async def _exists(self, path: str) -> bool:
        return (await self.ex.run(f"test -e {_q(path)}", timeout=10)).ok

    async def _backup(self, path: str, record: dict) -> str:
        bak = f"{path}.{MARK}.bak"
        if not await self._exists(bak):
            await self._run(f"cp -p {_q(path)} {_q(bak)}", what=f"backup {path}")
        record["modified_files"].append({"path": path, "backup": bak})
        return bak

    async def _reload(self, commands: list[str]) -> list[str]:
        out = []
        for c in commands:
            if self.adapter is not None and hasattr(self.adapter, "api"):
                try:
                    out.append(str(await self.adapter.api(c)).strip()[:120])
                    continue
                except Exception as exc:  # noqa: BLE001
                    out.append(f"{c}: adapter failed ({exc}); using CLI")
            cli = self.profile.fs_cli or ("asterisk" if self.flavor == "asterisk" else "fs_cli")
            if self.flavor == "asterisk":
                aconf = (self.profile.extra or {}).get("asterisk_conf")
                r = await self.ex.run(f"{cli}{' -C ' + _q(str(aconf)) if aconf else ''} -rx {_q(c)}", timeout=30)
            else:
                r = await self.ex.run(f"{cli} -x {_q(c)}", timeout=30)
            out.append((r.stdout or r.stderr).strip()[:120] or ("ok" if r.ok else "failed"))
        return out

    # -- public -------------------------------------------------------------------

    async def apply(self, plan: ProvisionPlan) -> dict:
        record = {"profile": self.profile.name, "flavor": self.flavor, "created_at": time.time(), "plan": asdict(plan), "created_files": [], "modified_files": [], "extensions": plan.extensions, "ring_group": plan.ring_group, "secret_names": [plan.secret_name(e) for e in plan.extensions], "notes": []}
        if self.flavor == "fusionpbx":
            await self._apply_fusionpbx(plan, record)
        elif self.flavor == "asterisk":
            await self._apply_asterisk(plan, record)
        else:
            await self._apply_freeswitch(plan, record)
        return record

    async def remove(self, record: dict) -> dict:
        notes = []
        if record.get("flavor") == "fusionpbx":
            notes += await self._remove_fusionpbx(record)
        for f in record.get("created_files", []):
            r = await self.ex.run(f"rm -f {_q(f)}", timeout=10)
            notes.append(f"removed {f}" if r.ok else f"could not remove {f}: {r.stderr.strip()[:100]}")
            await self.ex.run(f"rmdir {_q(str(Path(f).parent))} 2>/dev/null", timeout=10)  # only when we left it empty
        for m in record.get("modified_files", []):
            r = await self.ex.run(f"mv -f {_q(m['backup'])} {_q(m['path'])}", timeout=10)
            notes.append(f"restored {m['path']}" if r.ok else f"could not restore {m['path']}: {r.stderr.strip()[:100]}")
        if record.get("flavor") == "asterisk":
            notes += await self._reload(["module reload res_pjsip.so", "dialplan reload"])
        else:
            notes += await self._reload(["reloadxml"])
        return {"profile": record.get("profile"), "notes": notes}

    # -- FreeSWITCH (XML files) ---------------------------------------------------------

    def _conf_dir(self) -> str:
        cd = self.profile.conf_dir or ""
        if self.flavor == "asterisk" and (not cd or "freeswitch" in cd):
            return "/etc/asterisk"
        return cd or "/etc/freeswitch"

    async def _ensure_include(self, path: str, include_line: str, after_regex: str, record: dict) -> bool:
        text = await self._read(path)
        if include_line in text:
            return False
        m = re.search(after_regex, text)
        if not m:
            raise ProvisionError(f"{path}: cannot find where to add the include ({after_regex})")
        await self._backup(path, record)
        new = text[: m.end()] + "\n    " + include_line + text[m.end() :]
        await self._write(path, new.rstrip("\n"))
        return True

    async def _apply_freeswitch(self, plan: ProvisionPlan, record: dict) -> None:
        conf = self._conf_dir()
        dir_file = f"{conf}/directory/{plan.directory}.xml"
        dp_file = f"{conf}/dialplan/{plan.context}.xml"
        for f in (dir_file, dp_file):
            if not await self._exists(f):
                raise ProvisionError(f"{f} がありません。conf_dir（{conf}）とディレクトリ / コンテキスト名を確認してください")
        passwords = {}
        for ext in plan.extensions:
            pw = _password()
            passwords[ext] = pw
            self.secrets.set(plan.secret_name(ext), pw)
        users = []
        for ext in plan.extensions:
            users.append(
                f'  <user id="{ext}">\n'
                f'    <params><param name="password" value="{passwords[ext]}"/></params>\n'
                f'    <variables><variable name="user_context" value="{plan.context}"/><variable name="effective_caller_id_number" value="{ext}"/>'
                f'<variable name="effective_caller_id_name" value="Semishigure {ext}"/><variable name="call_timeout" value="30"/></variables>\n'
                f"  </user>"
            )
        user_xml = f"<!-- {MARK}: created by Semishigure; removed by `semishigure pbx unprovision` -->\n<include>\n" + "\n".join(users) + "\n</include>"
        user_path = f"{conf}/directory/{plan.directory}/{MARK}.xml"
        await self._write(user_path, user_xml)
        record["created_files"].append(user_path)
        if await self._ensure_include(dir_file, f'<X-PRE-PROCESS cmd="include" data="{plan.directory}/*.xml"/>', r"<users>", record):
            record["notes"].append(f"{dir_file} に {plan.directory}/*.xml の include を追加")
        bridge = ",".join(f"user/{a}@${{domain_name}}" for a in plan.answerers)
        limit = f'      <action application="limit" data="hash semishigure {MARK} {plan.group_limit} !USER_BUSY"/>\n' if plan.group_limit else ""
        direct = "|".join(re.escape(a) for a in plan.answerers)
        dp_xml = (
            f"<!-- {MARK}: created by Semishigure -->\n<include>\n"
            f'  <extension name="{MARK}-ring-group">\n'
            f'    <condition field="destination_number" expression="^{re.escape(plan.ring_group)}$">\n'
            f"{limit}"
            f'      <action application="set" data="ringback=${{us-ring}}"/>\n'
            f'      <action application="set" data="hangup_after_bridge=true"/>\n'
            f'      <action application="set" data="continue_on_fail=true"/>\n'
            f'      <action application="bridge" data="{bridge}"/>\n'
            f"    </condition>\n  </extension>\n"
            f'  <extension name="{MARK}-direct">\n'
            f'    <condition field="destination_number" expression="^({direct})$">\n'
            f'      <action application="set" data="hangup_after_bridge=true"/>\n'
            f'      <action application="bridge" data="user/$1@${{domain_name}}"/>\n'
            f"    </condition>\n  </extension>\n</include>"
        )
        dp_path = f"{conf}/dialplan/{plan.context}/{MARK}.xml"
        await self._write(dp_path, dp_xml)
        record["created_files"].append(dp_path)
        if await self._ensure_include(dp_file, f'<X-PRE-PROCESS cmd="include" data="{plan.context}/*.xml"/>', rf'<context name="{re.escape(plan.context)}"[^>]*>', record):
            record["notes"].append(f"{dp_file} に {plan.context}/*.xml の include を追加")
        record["notes"] += await self._reload(["reloadxml"])

    # -- Asterisk (pjsip.conf / extensions.conf) ----------------------------------------------

    async def _apply_asterisk(self, plan: ProvisionPlan, record: dict) -> None:
        conf = self._conf_dir()
        pj = f"{conf}/pjsip.conf"
        ext_conf = f"{conf}/extensions.conf"
        for f in (pj, ext_conf):
            if not await self._exists(f):
                raise ProvisionError(f"{f} がありません。conf_dir（{conf}）を確認してください")
        passwords = {}
        for ext in plan.extensions:
            pw = _password()
            passwords[ext] = pw
            self.secrets.set(plan.secret_name(ext), pw)
        ctx = "semishigure"
        lines = [f"; {MARK}: created by Semishigure; removed by `semishigure pbx unprovision`", f"[{MARK}-endpoint](!)", "type=endpoint", f"context={ctx}", "disallow=all", "allow=ulaw", "allow=alaw", "direct_media=no", "rtp_symmetric=yes", "force_rport=yes", "rewrite_contact=yes", "", f"[{MARK}-aor](!)", "type=aor", "max_contacts=2", "remove_existing=yes", "qualify_frequency=0", "", f"[{MARK}-auth](!)", "type=auth", "auth_type=userpass", ""]
        for ext in plan.extensions:
            lines += [f"[{ext}]({MARK}-endpoint)", f"auth={ext}", f"aors={ext}", f"callerid=Semishigure {ext} <{ext}>", "", f"[{ext}]({MARK}-auth)", f"username={ext}", f"password={passwords[ext]}", "", f"[{ext}]({MARK}-aor)", ""]
        pj_path = f"{conf}/{MARK}-pjsip.conf"
        await self._write(pj_path, "\n".join(lines))
        record["created_files"].append(pj_path)
        dial = "&".join(f"PJSIP/{a}" for a in plan.answerers)
        dp = [f"; {MARK}: created by Semishigure", f"[{ctx}]", f"exten => {plan.ring_group},1,NoOp(Semishigure ring group)"]
        if plan.group_limit:
            dp += [f" same => n,Set(GROUP()={MARK})", f" same => n,GotoIf($[${{GROUP_COUNT({MARK})}} > {plan.group_limit}]?busy)"]
        dp += [" same => n,Set(__SEMI_CALL=${PJSIP_HEADER(read,X-Semishigure-Call)})", f" same => n,Dial({dial},30,b({ctx}^predial^1))", " same => n,Hangup()", " same => n(busy),Busy()"]
        for a in plan.answerers:
            dp += [f"exten => {a},1,Set(__SEMI_CALL=${{PJSIP_HEADER(read,X-Semishigure-Call)}})", f" same => n,Dial(PJSIP/{a},30,b({ctx}^predial^1))", " same => n,Hangup()"]
        dp += ["exten => predial,1,NoOp(predial ${CHANNEL})", ' same => n,ExecIf($["${SEMI_CALL}" != ""]?Set(PJSIP_HEADER(add,X-Semishigure-Call)=${SEMI_CALL}))', " same => n,Return()"]
        dp_path = f"{conf}/{MARK}-extensions.conf"
        await self._write(dp_path, "\n".join(dp))
        record["created_files"].append(dp_path)
        for main, inc in ((pj, f"#include {MARK}-pjsip.conf"), (ext_conf, f"#include {MARK}-extensions.conf")):
            text = await self._read(main)
            if inc not in text:
                await self._backup(main, record)
                await self._write(main, text.rstrip("\n") + f"\n\n; {MARK}\n{inc}")
                record["notes"].append(f"{main} に {inc} を追加")
        record["notes"] += await self._reload(["module reload res_pjsip.so", "dialplan reload"])

    # -- FusionPBX (PostgreSQL) -----------------------------------------------------------

    async def _fusion_psql(self, plan: ProvisionPlan) -> str:
        """The psql command prefix (credentials from a secret or from config.conf)."""
        host, port, name, user, pw = "127.0.0.1", "5432", "fusionpbx", "fusionpbx", ""
        if plan.db_password_ref:
            pw = self.secrets.resolve(plan.db_password_ref if ":" in plan.db_password_ref else f"secret:{plan.db_password_ref}")
        else:
            r = await self.ex.run("cat /etc/fusionpbx/config.conf 2>/dev/null || sudo -n cat /etc/fusionpbx/config.conf", timeout=10)
            if not r.ok or "database.0" not in r.stdout:
                raise ProvisionError("/etc/fusionpbx/config.conf を読めません。データベースのパスワードを secret: で指定してください")
            for line in r.stdout.splitlines():
                m = re.match(r"\s*database\.0\.(\w+)\s*=\s*(.*)$", line)
                if not m:
                    continue
                k, v = m.group(1), m.group(2).strip()
                if k == "host":
                    host = v
                elif k == "port":
                    port = v
                elif k == "name":
                    name = v
                elif k == "username":
                    user = v
                elif k == "password":
                    pw = v
        return f"PGPASSWORD={_q(pw)} psql -X -q -v ON_ERROR_STOP=1 -h {_q(host)} -p {_q(port)} -U {_q(user)} -d {_q(name)} -At"

    async def _sql(self, psql: str, sql: str) -> str:
        marker = "SEMISHIGURE_SQL_EOF"
        r = await self.ex.run(f"{psql} <<'{marker}'\n{sql}\n{marker}\n", timeout=30)
        if not r.ok:
            raise ProvisionError(f"psql: {r.stderr.strip()[:300]}")
        return r.stdout.strip()

    async def _apply_fusionpbx(self, plan: ProvisionPlan, record: dict) -> None:
        domain = plan.domain or self.profile.domain
        if not domain:
            raise ProvisionError("FusionPBX のドメイン（テナント）名が必要です")
        psql = await self._fusion_psql(plan)
        domain_uuid = await self._sql(psql, f"select domain_uuid from v_domains where domain_name = {_sql_str(domain)};")
        if not domain_uuid:
            raise ProvisionError(f"ドメイン {domain} が FusionPBX にありません")
        fusion = {"domain_uuid": domain_uuid, "extension_uuids": [], "ring_group_uuid": None, "dialplan_uuid": None, "destination_uuids": [], "reused_extensions": [], "reused_ring_group": False}
        existing = await self._sql(psql, f"select extension || '|' || coalesce(password, '') from v_extensions where domain_uuid = '{domain_uuid}' and extension in ({', '.join(_sql_str(e) for e in plan.extensions)});")
        known = dict(line.split("|", 1) for line in existing.splitlines() if "|" in line)
        for ext in plan.extensions:
            if ext in known:
                # keep the extension the PBX already has; its password goes to the secret store
                self.secrets.set(plan.secret_name(ext), known[ext])
                fusion["reused_extensions"].append(ext)
                continue
            pw = _password()
            self.secrets.set(plan.secret_name(ext), pw)
            eu = str(uuid.uuid4())
            await self._sql(
                psql,
                "insert into v_extensions (extension_uuid, domain_uuid, extension, password, accountcode, effective_caller_id_name, effective_caller_id_number, directory_visible, directory_exten_visible, limit_max, user_context, call_timeout, enabled, description) values "
                f"('{eu}', '{domain_uuid}', {_sql_str(ext)}, {_sql_str(pw)}, {_sql_str(domain)}, {_sql_str('Semishigure ' + ext)}, {_sql_str(ext)}, 'true', 'true', {int(plan.max_calls)}, {_sql_str(domain)}, 30, 'true', {_sql_str(MARK)});",
            )
            fusion["extension_uuids"].append(eu)
        rg = await self._sql(psql, f"select ring_group_uuid from v_ring_groups where domain_uuid = '{domain_uuid}' and ring_group_extension = {_sql_str(plan.ring_group)};")
        if rg:
            fusion["reused_ring_group"] = True
            fusion["ring_group_uuid"] = rg
            await self._sql(psql, f"update v_ring_group_destinations set destination_enabled = 'true' where ring_group_uuid = '{rg}';")
            record["notes"].append(f"着信グループ {plan.ring_group} は既存のものを使用（宛先を有効化）")
        else:
            rgu, dpu = str(uuid.uuid4()), str(uuid.uuid4())
            xml = (
                f'<extension name="{MARK}" continue="" uuid="{dpu}">\n\t<condition field="destination_number" expression="^{re.escape(plan.ring_group)}$">\n'
                f'\t\t<action application="ring_ready" data=""/>\n\t\t<action application="set" data="ring_group_uuid={rgu}"/>\n'
                f'\t\t<action application="set" data="record_stereo=true"/>\n\t\t<action application="lua" data="app.lua ring_groups"/>\n\t</condition>\n</extension>'
            )
            await self._sql(
                psql,
                "insert into v_dialplans (dialplan_uuid, domain_uuid, app_uuid, dialplan_context, dialplan_name, dialplan_number, dialplan_continue, dialplan_xml, dialplan_order, dialplan_enabled, dialplan_description) values "
                f"('{dpu}', '{domain_uuid}', '{RING_GROUP_APP_UUID}', {_sql_str(domain)}, {_sql_str(MARK)}, {_sql_str(plan.ring_group)}, 'false', {_sql_str(xml)}, 101, 'true', {_sql_str('created by Semishigure')});",
            )
            await self._sql(
                psql,
                "insert into v_ring_groups (ring_group_uuid, domain_uuid, ring_group_name, ring_group_extension, ring_group_strategy, ring_group_context, ring_group_call_timeout, ring_group_enabled, ring_group_description, dialplan_uuid) values "
                f"('{rgu}', '{domain_uuid}', {_sql_str(MARK)}, {_sql_str(plan.ring_group)}, 'simultaneous', {_sql_str(domain)}, 30, 'true', {_sql_str('created by Semishigure')}, '{dpu}');",
            )
            for a in plan.answerers:
                du = str(uuid.uuid4())
                await self._sql(psql, f"insert into v_ring_group_destinations (ring_group_destination_uuid, domain_uuid, ring_group_uuid, destination_number, destination_delay, destination_timeout, destination_enabled) values ('{du}', '{domain_uuid}', '{rgu}', {_sql_str(a)}, 0, 30, 'true');")
                fusion["destination_uuids"].append(du)
            fusion["ring_group_uuid"], fusion["dialplan_uuid"] = rgu, dpu
        record["fusion"] = fusion
        record["notes"] += await self._fusion_refresh(domain, plan.extensions)

    async def _fusion_refresh(self, domain: str, exts: list[str]) -> list[str]:
        files = " ".join(_q(f"/var/cache/fusionpbx/directory.{e}@{domain}") for e in exts) + " " + _q(f"/var/cache/fusionpbx/dialplan.{domain}")
        await self.ex.run(f"rm -f {files} 2>/dev/null || sudo -n rm -f {files}", timeout=10)
        notes = await self._reload(["memcache flush", "reloadxml"])
        return [n for n in notes if "Command not found" not in n]  # mod_memcache is optional

    async def _remove_fusionpbx(self, record: dict) -> list[str]:
        plan = ProvisionPlan(**record.get("plan", {}))
        fusion = record.get("fusion") or {}
        psql = await self._fusion_psql(plan)
        notes = []
        if fusion.get("extension_uuids"):
            ids = ", ".join(f"'{u}'" for u in fusion["extension_uuids"])
            await self._sql(psql, f"delete from v_extensions where extension_uuid in ({ids});")
            notes.append(f"deleted {len(fusion['extension_uuids'])} extensions")
        if fusion.get("ring_group_uuid") and not fusion.get("reused_ring_group"):
            await self._sql(psql, f"delete from v_ring_group_destinations where ring_group_uuid = '{fusion['ring_group_uuid']}';")
            await self._sql(psql, f"delete from v_ring_groups where ring_group_uuid = '{fusion['ring_group_uuid']}';")
            if fusion.get("dialplan_uuid"):
                await self._sql(psql, f"delete from v_dialplan_details where dialplan_uuid = '{fusion['dialplan_uuid']}';")
                await self._sql(psql, f"delete from v_dialplans where dialplan_uuid = '{fusion['dialplan_uuid']}';")
            notes.append("deleted the ring group and its dialplan")
        domain = plan.domain or self.profile.domain
        notes += await self._fusion_refresh(domain, plan.extensions)
        return notes
