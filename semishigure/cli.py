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
from semishigure.core.engine import SipEngine
from semishigure.media.wav import synth_speech_like, write_wav
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
        if any(c.record.state.value == "ESTABLISHED" for c in calls):
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
        Path(args.report).write_text(json.dumps(snap, indent=2, ensure_ascii=False))
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
    c.set_defaults(func=lambda a: asyncio.run(_cmd_call(a)))

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
