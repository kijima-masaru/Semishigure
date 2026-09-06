"""semishigure command line."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
import time
from pathlib import Path

from semishigure import __version__
from semishigure.core.call import CallRecord
from semishigure.core.controller import parse_schedule, preset_schedule
from semishigure.core.engine import SipEngine
from semishigure.core.run import ProdConfirmationRequired, Run
from semishigure.core.store import RunStore
from semishigure.media.wav import synth_speech_like, write_wav
from semishigure.pbx.adapter import make_adapter
from semishigure.pbx.profile import DEFAULT_DIR, PbxProfile, ProfileStore, build_executor
from semishigure.scenario.model import load_scenario
from semishigure.secrets import SecretError, SecretStore

log = logging.getLogger("semishigure")


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")


# ---------------------------------------------------------------------------
# semishigure call  (stage 1: one or a few calls through the PBX)
# ---------------------------------------------------------------------------


async def _cmd_call(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    if args.pbx_host:
        scenario.pbx.host = args.pbx_host
    if args.domain:
        scenario.pbx.domain = args.domain
    if args.destination:
        scenario.caller.destination = args.destination
    if not scenario.pbx.domain:
        print("error: pbx.domain is required (SIP domain name, not an IP)", file=sys.stderr)
        return 2
    record_dir = Path(args.record_rx) if args.record_rx else None
    if record_dir:
        record_dir.mkdir(parents=True, exist_ok=True)

    def on_event(rec: CallRecord, event: str) -> None:
        log.info("[%s %s->%s] %s", rec.role.value, rec.local_user, rec.remote_user, event)

    engine = SipEngine(scenario, SecretStore(), record_rx_dir=record_dir, sip_trace=args.sip_trace, on_event=on_event)
    started = time.time()
    exit_code = 0
    try:
        await engine.start()
        print(f"local ip {engine.local_ip}  caller port {scenario.pbx.caller_port}  answerer port {scenario.pbx.answerer_port}")
        for user, ok in engine.registration_results.items():
            ext = engine.answerer.extensions[user]  # type: ignore[union-attr]
            reg = ext.registration
            print(f"REGISTER {user}: {'ok' if ok else 'FAILED'} ({reg.last_status} in {reg.register_rtt_ms} ms, expires {reg.granted_expires}s)" if reg else f"REGISTER {user}: {ok}")
        if not all(engine.registration_results.values()) and not args.ignore_register_failure:
            print("aborting: registration failed (use --ignore-register-failure to continue)", file=sys.stderr)
            return 3

        calls = []
        for i in range(args.calls):
            call = await engine.place_call()
            calls.append(call)
            r = call.record
            print(f"call {i + 1}: {r.state.value} reason={r.end_reason or '-'} status={r.final_status} 180={r.invite_to_180_ms}ms 200={r.invite_to_200_ms}ms codec={r.codec}")
            if i + 1 < args.calls and args.interval > 0:
                await asyncio.sleep(args.interval)
        if scenario.caller.steps and not args.ignore_steps:
            print("running scenario steps: " + ", ".join(next(iter(st)) if isinstance(st, dict) else str(st) for st in scenario.caller.steps))
            await asyncio.gather(*(c.run_steps(scenario.caller.steps, max_seconds=args.duration) for c in calls if c.record.state.value == "ESTABLISHED"), return_exceptions=True)
        elif any(c.record.state.value == "ESTABLISHED" for c in calls):
            print(f"holding {args.duration:.0f}s ...")
            deadline = time.monotonic() + args.duration
            while time.monotonic() < deadline:
                await asyncio.sleep(min(1.0, deadline - time.monotonic()))
                if not engine.active_calls:
                    print("all calls ended by the far end")
                    break
        await engine.hangup_all("duration_elapsed")
        await asyncio.sleep(0.3)  # let the answerer side settle
    except SecretError as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 2
    finally:
        await engine.shutdown()

    snap = engine.snapshot()
    snap["started_at"] = started
    snap["finished_at"] = time.time()
    _print_report(snap)
    if args.report:
        Path(args.report).write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report written to {args.report}")
    ok = all(r["end_reason"] in ("duration_elapsed", "remote_bye", "local_bye") and r["invite_to_200_ms"] is not None for r in snap["caller_calls"])
    return exit_code if exit_code else (0 if ok else 1)


def _fmt(v, suffix: str = "") -> str:
    return "-" if v is None else f"{v}{suffix}"


def _print_report(snap: dict) -> None:
    print()
    print("=== caller (UAC) ===")
    for r in snap["caller_calls"]:
        m = r.get("media") or {}
        print(
            f"  {r['local']}->{r['remote']} {r['state']} reason={r['end_reason']} status={r['final_status']} auth={r['auth_rounds']} codec={r['codec']}\n"
            f"    INVITE->100 {_fmt(r['invite_to_100_ms'], 'ms')}  ->180 {_fmt(r['invite_to_180_ms'], 'ms')}  ->200 {_fmt(r['invite_to_200_ms'], 'ms')}  duration {_fmt(r['duration_s'], 's')}\n"
            f"    RTP tx {m.get('tx_packets')} pkts (first {_fmt(r['established_to_first_rtp_tx_ms'], 'ms')} after 200)  rx {m.get('rx_packets')} pkts lost {m.get('rx_lost')} (first {_fmt(r['established_to_first_rtp_rx_ms'], 'ms')} after 200)\n"
            f"    tx lateness max {m.get('tx_late_max_ms')}ms mean {m.get('tx_late_mean_ms')}ms >5ms {m.get('tx_late_over_5ms')} >20ms {m.get('tx_late_over_20ms')}  rx level rms {m.get('rx_level_rms')} (peak {m.get('rx_level_peak_rms')})"
        )
    print("=== answerer (UAS) ===")
    for u, reg in snap["registrations"].items():
        print(f"  {u}: {reg['state']} rtt={_fmt(reg['rtt_ms'], 'ms')} expires={reg['expires']} busy_rejects={reg['busy_rejects']}")
    for r in snap["answerer_calls"]:
        m = r.get("media") or {}
        print(
            f"  ext {r['extension']} <- {r['remote']} {r['state']} reason={r['end_reason']} codec={r['codec']}\n"
            f"    INVITE->180 {_fmt(r['invite_to_180_ms'], 'ms')}  ->200 {_fmt(r['invite_to_200_ms'], 'ms')}  duration {_fmt(r['duration_s'], 's')}\n"
            f"    RTP tx {m.get('tx_packets')} pkts (first {_fmt(r['established_to_first_rtp_tx_ms'], 'ms')} after ACK)  rx {m.get('rx_packets')} pkts lost {m.get('rx_lost')} (first {_fmt(r['established_to_first_rtp_rx_ms'], 'ms')} after ACK)\n"
            f"    tx lateness max {m.get('tx_late_max_ms')}ms mean {m.get('tx_late_mean_ms')}ms >5ms {m.get('tx_late_over_5ms')} >20ms {m.get('tx_late_over_20ms')}  rx level rms {m.get('rx_level_rms')} (peak {m.get('rx_level_peak_rms')})"
        )
    print("=== media engine ===")
    print(f"  {json.dumps(snap['media'])}")


# ---------------------------------------------------------------------------
# semishigure load  (stage 2: headless load run with a step schedule)
# ---------------------------------------------------------------------------


async def _cmd_load(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    if args.pbx_host:
        scenario.pbx.host = args.pbx_host
    record_dir = Path(args.record_rx) if args.record_rx else None
    if record_dir:
        record_dir.mkdir(parents=True, exist_ok=True)
    store = None if args.no_store else RunStore()
    try:
        run = Run(
            scenario,
            name=args.name or "",
            store=store,
            target=args.target,
            ramp_rate=args.ramp,
            call_duration=args.duration,
            max_concurrency=args.max_concurrency,
            max_total_calls=args.max_total,
            confirm_prod=args.confirm_prod,
            record_rx_dir=record_dir,
            sip_trace=args.sip_trace,
            on_event=lambda kind, ev: log.info("%s: %s", kind, ev.get("message", ev)),
            profile=ProfileStore().get(args.pbx_profile) if args.pbx_profile else None,
            monitor_enabled=not args.no_monitor,
        )
        if args.pbx_profile and run.profile is None:
            print(f"error: PBX profile {args.pbx_profile!r} not found in {ProfileStore().path}", file=sys.stderr)
            return 2
    except ProdConfirmationRequired as exc:
        print(f"error: {exc} (pass --confirm-prod)", file=sys.stderr)
        return 2
    steps = None
    if args.preset:
        preset = scenario.load.presets.get(args.preset)
        if preset is None:
            print(f"error: preset {args.preset!r} not in scenario (have {list(scenario.load.presets)})", file=sys.stderr)
            return 2
        steps, _, _ = preset_schedule(preset, run.config.call_duration)
    elif args.schedule:
        steps = parse_schedule(args.schedule, run.config.call_duration)
    exit_code = 0
    try:
        await run.start(ignore_register_failure=args.ignore_register_failure)
        print(f"run {run.name} (id {run.run_id}) local {run.engine.local_ip} -> {scenario.pbx.host}:{scenario.pbx.sip_port} domain {scenario.pbx.domain}")
        for user, ok in run.engine.registration_results.items():
            print(f"  REGISTER {user}: {'ok' if ok else 'FAILED'}")
        if steps:
            total = sum(s.seconds for s in steps)
            print("schedule: " + " -> ".join(f"N={s.target} for {s.seconds:.0f}s" for s in steps) + f" (total {total:.0f}s)")
            run.controller.run_schedule(steps, then_target=0)
        deadline = time.monotonic() + (args.time if args.time else (sum(s.seconds for s in steps) + args.drain_time if steps else 0))
        last = ""
        while True:
            await asyncio.sleep(1.0)
            st = run.controller.state()
            s = run.stats.summary()
            line = f"t={s['elapsed_s']:6.1f}s target={st['target']:3d} est={st['established']:3d} pend={st['pending']:2d} started={s['calls_started']} failed={s['calls_failed']} cps={s['cps']} p95(200)={s['invite_to_200_ms']['p95']} rtp_late_max={s['rtp']['late_max_ms']}ms"
            if run.monitor is not None and run.monitor.state.last:
                m = run.monitor.state.last
                line += f" | pbx ch={m.get('channels')} cpu={m.get('cpu')}% nlwp={m.get('nlwp')}"
            if st["backoff_reason"]:
                line += f"  BACKOFF: {st['backoff_reason']}"
            if line != last:
                print(line, flush=True)
                last = line
            if deadline and time.monotonic() >= deadline:
                if steps and (st["established"] + st["pending"]) > 0 and time.monotonic() < deadline + args.drain_time:
                    continue
                break
            if steps and run.controller.schedule_index < 0 and st["target"] == 0 and st["established"] + st["pending"] == 0:
                break
    except KeyboardInterrupt:
        print("interrupted: hanging up all calls")
        exit_code = 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 3
    finally:
        await run.stop()
        if store is not None:
            store.close()
    summ = run.summary()
    print(json.dumps({k: v for k, v in summ.items() if k != "plugin_rows"}, indent=2, ensure_ascii=False))
    if summ.get("plugin_rows"):
        print("=== plugin rows ===")
        for label, value in summ["plugin_rows"]:
            print(f"  {label}: {value}")
    if args.report:
        Path(args.report).write_text(json.dumps({"summary": summ, "series": list(run.stats.series), "events": list(run.stats.events)}, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"report written to {args.report}")
    return exit_code


async def _cmd_pbx(args: argparse.Namespace) -> int:
    store = ProfileStore()
    if args.pbx_cmd == "list":
        profiles = store.load_all()
        if not profiles:
            print(f"no profiles in {store.path} (see examples/pbx_profiles.example.yaml)")
        for p in profiles:
            print(f"{p.name:16} {p.type:10} {p.host}:{p.sip_port} domain={p.domain} env={p.environment} executor={p.executor}" + (f" ssh={p.ssh_user}@{p.ssh_host or p.host}:{p.ssh_port}" if p.executor == "ssh" else ""))
        return 0
    if args.pbx_cmd == "init":
        example = Path(__file__).resolve().parent.parent / "examples" / "pbx_profiles.example.yaml"
        if store.path.exists() and not args.force:
            print(f"{store.path} exists (use --force to overwrite)")
            return 1
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {store.path}")
        return 0
    profile = store.get(args.name)
    if profile is None:
        print(f"error: profile {args.name!r} not found in {store.path}", file=sys.stderr)
        return 2
    if args.pbx_cmd == "show":
        print(json.dumps(profile.public(), indent=2, ensure_ascii=False))
        return 0
    if args.pbx_cmd == "test":
        return await _pbx_test(profile)
    if args.pbx_cmd in ("provision", "unprovision"):
        return await _pbx_provision(profile, args)
    return 0


async def _pbx_provision(profile: PbxProfile, args: argparse.Namespace) -> int:
    """Create (or remove) the extensions and the ring group on the PBX."""
    from semishigure.pbx.provision import Provisioner, ProvisionError, ProvisionPlan, ProvisionStore

    secrets = SecretStore()
    pstore = ProvisionStore()
    executor = build_executor(profile, secrets)
    await executor.connect()
    try:
        if args.pbx_cmd == "unprovision":
            record = pstore.get(profile.name)
            if record is None:
                print(f"{profile.name}: nothing to remove (not provisioned)")
                return 1
            res = await Provisioner(profile, executor, secrets, None).remove(record)
            pstore.delete(profile.name)
            for n in res["notes"]:
                print(f"  {n}")
            print(f"{profile.name}: removed")
            return 0
        if pstore.get(profile.name):
            print(f"{profile.name}: already provisioned (run `semishigure pbx unprovision {profile.name}` first)", file=sys.stderr)
            return 1
        plan = ProvisionPlan(caller=args.caller, answerers=[a.strip() for a in args.answerers.split(",") if a.strip()], ring_group=args.ring_group, max_calls=args.max_calls, group_limit=args.group_limit, secret_prefix=args.secret_prefix, domain=args.domain or profile.domain, context=args.context, directory=args.directory, db_password_ref=args.db_password_ref)
        adapter = None
        try:
            adapter = make_adapter(profile, executor, secrets)
            await adapter.connect()
        except Exception:  # noqa: BLE001
            adapter = None
        try:
            record = await Provisioner(profile, executor, secrets, adapter).apply(plan)
        finally:
            if adapter is not None:
                await adapter.close()
        pstore.put(profile.name, record)
        print(f"{profile.name}: created extensions {', '.join(record['extensions'])} and ring group {record['ring_group']}")
        print(f"  passwords stored as secret:{', secret:'.join(record['secret_names'])}")
        for n in record["notes"]:
            print(f"  {n}")
        return 0
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await executor.close()


async def _pbx_test(profile: PbxProfile) -> int:
    t0 = time.monotonic()
    adapter = make_adapter(profile, build_executor(profile))
    try:
        await adapter.connect()
        print(f"connected via {profile.executor} in {(time.monotonic() - t0) * 1000:.0f} ms: {adapter.describe()}")
        st = await adapter.status()
        print(f"version: {st.version}")
        print(f"uptime: {st.uptime}")
        print(f"sessions: {st.sessions} (peak {st.sessions_peak}) max {st.sessions_max}  sps max {st.sps_max}")
        print(f"channels: {await adapter.channels_count()}")
        regs = await adapter.registrations() if hasattr(adapter, "registrations") else []
        print(f"registrations: {', '.join(regs) if regs else '(none)'}")
        pm = await adapter.process_metrics()
        print(f"process: pid={pm.pid} cpu={pm.cpu_percent}% mem={pm.mem_percent}% threads={pm.threads} rss={pm.rss_kb}kB")
        for t in pm.top_threads[:5]:
            print(f"  tid {t['tid']:>7} cpu {t['cpu']:>5} {t['name']}")
        tail = await adapter.log_tail(3)
        print("log tail:")
        for ln in tail:
            print("  " + ln[:140])
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await adapter.close()


def _cmd_runs(args: argparse.Namespace) -> int:
    store = RunStore()
    for r in store.list_runs(limit=args.limit):
        s = r.get("summary") or {}
        i200 = s.get("invite_to_200_ms") or {}
        print(f"#{r['id']:<4} {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['started_at'] or 0))}  {r['name']:<45} {r['scenario_name']:<24} calls {s.get('calls_started', '-'):>4} failed {s.get('calls_failed', '-'):>3}  p95(200) {i200.get('p95', '-')} ms")
    store.close()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from semishigure.report.xlsx import build_workbook

    store = RunStore()
    ids = [int(x) for x in args.runs.replace(" ", "").split(",") if x] if args.runs else [r["id"] for r in store.list_runs(limit=args.limit)][::-1]
    runs = []
    for rid in ids:
        r = store.get_run(rid)
        if r is None:
            print(f"warning: run #{rid} not found", file=sys.stderr)
            continue
        runs.append(r)
    store.close()
    if not runs:
        print("no runs to export", file=sys.stderr)
        return 1
    wb = build_workbook(runs)
    wb.save(args.output)
    print(f"wrote {args.output} ({len(runs)} run(s): {', '.join('#' + str(r['id']) for r in runs)})")
    return 0


def _cmd_plugins(args: argparse.Namespace) -> int:
    from semishigure.plugins.registry import describe_builtin

    for d in describe_builtin():
        print(f"{d['name']:16} {d.get('description') or d.get('error')}")
        print(f"{'':16} {d['class']}")
    print("\nexternal: plugins:\n  myname:\n    module: package.module:ClassName")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from semishigure.api.app import create_app

    scenarios = Path(args.scenarios) if args.scenarios else (Path("examples") if Path("examples").is_dir() else DEFAULT_DIR / "scenarios")
    print(f"scenarios: {scenarios}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {args.host}: the UI has no authentication and the secrets API stores passwords; put TLS and access control in front of it", file=sys.stderr)
    app = create_app(scenario_dir=scenarios, store=None if args.no_store else RunStore())
    uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")
    return 0


# ---------------------------------------------------------------------------
# audio / secret helpers
# ---------------------------------------------------------------------------


def _cmd_audio_synth(args: argparse.Namespace) -> int:
    src = synth_speech_like(args.seconds, base_hz=args.pitch, seed=args.seed)
    write_wav(args.output, src.pcm16)
    print(f"wrote {args.output}: {src.duration_s:.1f}s 8kHz/16bit/mono")
    return 0


def _cmd_secret(args: argparse.Namespace) -> int:
    store = SecretStore()
    if args.secret_cmd == "set":
        value = args.value if args.value is not None else getpass.getpass(f"value for {args.name}: ")
        store.set(args.name, value)
        print(f"stored secret:{args.name} in {store.file}")
    elif args.secret_cmd == "list":
        for n in store.names():
            print(n)
    elif args.secret_cmd == "delete":
        print("deleted" if store.delete(args.name) else "not found")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="semishigure", description="PBX concurrent-call load tester")
    p.add_argument("--version", action="version", version=f"semishigure {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("call", help="place one or more test calls through the PBX (stage 1 check)")
    c.add_argument("scenario", help="scenario YAML")
    c.add_argument("--calls", type=int, default=1)
    c.add_argument("--interval", type=float, default=1.0, help="seconds between calls")
    c.add_argument("--duration", type=float, default=20.0, help="seconds to hold each call")
    c.add_argument("--destination")
    c.add_argument("--pbx-host")
    c.add_argument("--domain")
    c.add_argument("--record-rx", help="directory to write received audio as WAV")
    c.add_argument("--report", help="write JSON report to this file")
    c.add_argument("--sip-trace", action="store_true", help="log every SIP message (needs -vv)")
    c.add_argument("--ignore-register-failure", action="store_true")
    c.add_argument("--ignore-steps", action="store_true", help="hold for --duration instead of running caller.steps")
    c.set_defaults(func=lambda a: asyncio.run(_cmd_call(a)))

    ld = sub.add_parser("load", help="run a load test headless (target / schedule / preset)")
    ld.add_argument("scenario")
    ld.add_argument("--name")
    ld.add_argument("--target", type=int, help="initial target concurrency (default: scenario load.target_concurrency)")
    ld.add_argument("--schedule", help="step schedule, e.g. '5:60,20:60,8:60' (target:seconds)")
    ld.add_argument("--preset", help="scenario preset name (A, B, ...)")
    ld.add_argument("--ramp", type=float, help="calls per second when ramping up")
    ld.add_argument("--duration", type=float, help="call duration seconds")
    ld.add_argument("--time", type=float, help="run time in seconds when no schedule is given (0 = until Ctrl-C)")
    ld.add_argument("--drain-time", type=float, default=15.0, help="extra seconds to wait for calls to end after the schedule")
    ld.add_argument("--max-concurrency", type=int, default=50)
    ld.add_argument("--max-total", type=int)
    ld.add_argument("--confirm-prod", action="store_true")
    ld.add_argument("--pbx-host")
    ld.add_argument("--pbx-profile", help="PBX profile name (overrides the scenario's pbx_profile)")
    ld.add_argument("--no-monitor", action="store_true", help="do not connect to the PBX host for monitoring")
    ld.add_argument("--record-rx")
    ld.add_argument("--report")
    ld.add_argument("--no-store", action="store_true", help="do not save the run to SQLite")
    ld.add_argument("--sip-trace", action="store_true")
    ld.add_argument("--ignore-register-failure", action="store_true")
    ld.set_defaults(func=lambda a: asyncio.run(_cmd_load(a)))

    sv = sub.add_parser("serve", help="start the web UI / API server")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--scenarios", default=None, help="directory with scenario YAML files (default: $SEMISHIGURE_HOME/scenarios, or ./examples when it exists)")
    sv.add_argument("--no-store", action="store_true")
    sv.set_defaults(func=_cmd_serve)

    rs = sub.add_parser("runs", help="list saved runs")
    rs.add_argument("--limit", type=int, default=30)
    rs.set_defaults(func=_cmd_runs)

    rp = sub.add_parser("report", help="export the record sheet (xlsx) for saved runs")
    rp.add_argument("--runs", help="run ids, e.g. 3,4,5 (default: the latest --limit runs)")
    rp.add_argument("--limit", type=int, default=10)
    rp.add_argument("-o", "--output", default="semishigure-report.xlsx")
    rp.set_defaults(func=_cmd_report)

    pl = sub.add_parser("plugins", help="list built-in plugins")
    pl.set_defaults(func=_cmd_plugins)

    px = sub.add_parser("pbx", help="PBX profiles (connection, monitoring)")
    psub = px.add_subparsers(dest="pbx_cmd", required=True)
    psub.add_parser("list")
    pi = psub.add_parser("init", help="write the example profiles file")
    pi.add_argument("--force", action="store_true")
    ps = psub.add_parser("show")
    ps.add_argument("name")
    pt = psub.add_parser("test", help="connect and read status / metrics / log")
    pt.add_argument("name")
    pp = psub.add_parser("provision", help="create the load-test extensions and ring group on the PBX")
    pp.add_argument("name")
    pp.add_argument("--caller", default="9100")
    pp.add_argument("--answerers", default="9001,9002,9003,9004", help="comma separated")
    pp.add_argument("--ring-group", default="8001")
    pp.add_argument("--max-calls", type=int, default=5)
    pp.add_argument("--group-limit", type=int, default=20, help="0 = no limit")
    pp.add_argument("--secret-prefix", default="ext", help="passwords go to secret:<prefix>_<extension>")
    pp.add_argument("--domain", default="", help="FusionPBX tenant (default: profile domain)")
    pp.add_argument("--context", default="default", help="FreeSWITCH dialplan context")
    pp.add_argument("--directory", default="default", help="FreeSWITCH directory name")
    pp.add_argument("--db-password-ref", default="", help="FusionPBX: secret:NAME for the database")
    pu = psub.add_parser("unprovision", help="remove what `pbx provision` created")
    pu.add_argument("name")
    px.set_defaults(func=lambda a: asyncio.run(_cmd_pbx(a)))

    a = sub.add_parser("audio", help="audio helpers")
    asub = a.add_subparsers(dest="audio_cmd", required=True)
    s = asub.add_parser("synth", help="generate a speech-like test WAV (8kHz/16bit/mono)")
    s.add_argument("output")
    s.add_argument("--seconds", type=float, default=60.0)
    s.add_argument("--pitch", type=float, default=180.0)
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(func=_cmd_audio_synth)

    sec = sub.add_parser("secret", help="manage the encrypted secret store")
    ssub = sec.add_subparsers(dest="secret_cmd", required=True)
    st = ssub.add_parser("set")
    st.add_argument("name")
    st.add_argument("value", nargs="?")
    ssub.add_parser("list")
    d = ssub.add_parser("delete")
    d.add_argument("name")
    sec.set_defaults(func=_cmd_secret)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
