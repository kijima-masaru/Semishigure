"""Pre-run check (design §3.7 事前チェック): registrations, one call, media both ways,
PBX connectivity and plugin/log expectations, reported as pass/fail items."""

from __future__ import annotations

import asyncio
import time

from semishigure.core.call import CallState
from semishigure.core.run import Run


async def precheck(run: Run, hold_seconds: float = 6.0, ignore_register_failure: bool = True) -> dict:
    items: list[dict] = []
    t0 = time.time()

    def add(name: str, ok: bool, detail: str = "") -> None:
        items.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        await run.start(ignore_register_failure=ignore_register_failure)
    except Exception as exc:  # noqa: BLE001
        add("起動", False, str(exc))
        return {"ok": False, "items": items, "elapsed_s": round(time.time() - t0, 1)}
    try:
        # the load controller must not manage (drain) the single test call
        await run.controller.stop(hangup=False)
        regs = run.engine.registration_results
        for user, ok in regs.items():
            reg = run.engine.answerer.extensions[user].registration if run.engine.answerer else None
            add(f"REGISTER {user}", ok, f"{reg.last_status} in {reg.register_rtt_ms} ms, expires {reg.granted_expires}s" if reg else "")
        if run.monitor is not None:
            await asyncio.sleep(0.5)
            last = run.monitor.state.last
            add("PBX 監視", bool(last), f"channels={last.get('channels')} cpu={last.get('cpu')} nlwp={last.get('nlwp')} ({run.adapter.describe().get('esl') if run.adapter else ''})")
        elif run.monitor_error:
            add("PBX 監視", False, run.monitor_error)
        call = await run.engine.place_call()
        rec = call.record
        add("発信 → 応答", rec.state == CallState.ESTABLISHED, f"{rec.end_reason or ''} status={rec.final_status} INVITE→200 {rec.invite_to_200_ms} ms auth={rec.auth_rounds}")
        if rec.state == CallState.ESTABLISHED:
            await asyncio.sleep(hold_seconds)
            m = rec.media
            add("RTP 送出（発信側）", m is not None and m.tx_packets > 20, f"tx {m.tx_packets if m else 0} pkts, late max {round(m.tx_late_max * 1000, 2) if m else '-'} ms")
            add("RTP 受信（発信側）", m is not None and m.rx_packets > 20 and m.rx_level_peak_rms > 100, f"rx {m.rx_packets if m else 0} pkts, level {round(m.rx_level_peak_rms) if m else 0}")
            inbound = [r for r in (run.engine.answerer.records if run.engine.answerer else []) if r.t_established is not None]
            if inbound:
                ir = inbound[-1]
                im = ir.media
                add(f"応答側 {ir.extension} の RTP 受信", im is not None and im.rx_packets > 20 and im.rx_level_peak_rms > 100, f"rx {im.rx_packets if im else 0} pkts, level {round(im.rx_level_peak_rms) if im else 0}")
            else:
                add("応答側の通話", False, "応答側に確立した通話がない（PBX が外部の電話に繋いだ、または内線が未登録）")
            await call.hangup("precheck")
            add("BYE", rec.state == CallState.DONE and rec.final_status == 200, f"{rec.end_reason} status={rec.final_status}")
        if run.monitor is not None:
            await asyncio.sleep(1.0)
            hc = dict(run.monitor.state.hangup_causes)
            add("PBX イベント（ESL / AMI）", sum(run.monitor.state.esl_events.values()) > 0, ", ".join(f"{k}:{v}" for k, v in run.monitor.state.esl_events.items()) + (f" hangup: {hc}" if hc else ""))
        if run.plugins is not None and len(run.plugins):
            snap = run.plugins.snapshot()
            for name, p in snap.items():
                errors = p.get("errors") or []
                counters = p.get("counters") or {}
                add(f"プラグイン {name}", not errors, ", ".join(f"{k}={v}" for k, v in counters.items()) if counters else ("エラーなし" if not errors else "; ".join(errors)))
    finally:
        await run.stop()
    return {"ok": all(i["ok"] for i in items), "items": items, "elapsed_s": round(time.time() - t0, 1), "run_id": run.run_id}
