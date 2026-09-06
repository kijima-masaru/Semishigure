"""Load controller against the in-process answerer (no PBX): the caller
endpoint sends INVITEs straight to the answerer endpoint."""

import asyncio
from pathlib import Path

import pytest

from semishigure.core.run import ProdConfirmationRequired, Run
from semishigure.core.store import RunStore
from semishigure.scenario.model import scenario_from_dict


def _scenario(env: str = "dev") -> dict:
    return {
        "name": "loop",
        "pbx": {"host": "127.0.0.1", "sip_port": 5181, "domain": "loop.test", "local_ip": "127.0.0.1", "caller_port": 5171, "answerer_port": 5181, "rtp_port_start": 22000, "rtp_port_end": 22199, "environment": env},
        "caller": {"auth_user": "9100", "auth_password_ref": "literal:x", "destination": "9001", "audio": "synth:3"},
        "answerer": {"extensions": [{"user": "9001", "password_ref": "literal:x", "max_calls": 30}], "audio": "synth:3"},
        "load": {"target_concurrency": 0, "ramp_rate": "20/s", "call_duration": "60s"},
    }


async def _wait(cond, timeout=8.0):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_target_tracking_and_drain(tmp_path: Path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run = Run(scenario_from_dict(_scenario()), store=store, target=0, ramp_rate=20, call_duration=60)
    run.controller.config.drain_rate = 20
    await run.start(ignore_register_failure=True)  # the answerer endpoint answers REGISTER with 405
    c = run.controller
    try:
        c.set_target(5)
        assert await _wait(lambda: c.state()["established"] == 5)
        c.set_target(8)
        assert await _wait(lambda: c.state()["established"] == 8)
        c.set_target(2)
        assert await _wait(lambda: c.state()["established"] == 2 and c.state()["pending"] == 0)
        await asyncio.sleep(1.2)
        st = c.state()
        assert st["established"] == 2
        assert run.stats.calls_started == 8
        assert run.stats.end_by_reason.get("drain") == 6
        assert run.stats.calls_failed == 0
        # schedule: 4 for 1 s, then 1
        from semishigure.core.controller import parse_schedule

        c.run_schedule(parse_schedule("4:1,1:1"), then_target=0)
        assert await _wait(lambda: c.state()["established"] == 4)
        assert await _wait(lambda: c.state()["established"] == 1, timeout=4)
        assert await _wait(lambda: c.state()["established"] == 0 and c.state()["target"] == 0, timeout=4)
    finally:
        await run.stop()
    s = run.summary()
    # 8 during the manual steps, then 2 more for the schedule's 4 (2 were still up)
    assert s["calls_started"] == 10 and s["calls_failed"] == 0
    assert s["answered_by_ext"]["9001"] == 10
    assert s["invite_to_200_ms"]["n"] == 10
    assert len(run.stats.series) >= 3
    saved = store.get_run(run.run_id)
    assert saved["summary"]["calls_started"] == 10 and len(saved["samples"]) >= 3
    assert any(c["role"] == "caller" for c in saved["calls"])
    store.close()


async def test_call_duration_expiry_and_refill():
    run = Run(scenario_from_dict(_scenario()), target=3, ramp_rate=20, call_duration=1.0)
    await run.start(ignore_register_failure=True)
    try:
        assert await _wait(lambda: run.controller.state()["established"] == 3)
        await asyncio.sleep(2.5)
        st = run.controller.state()
        assert st["established"] == 3
        assert run.stats.end_by_reason.get("duration_elapsed", 0) >= 3
    finally:
        await run.stop()


async def test_backoff_on_consecutive_busy():
    sc = _scenario()
    sc["answerer"]["extensions"][0]["max_calls"] = 2
    run = Run(scenario_from_dict(sc), target=6, ramp_rate=20, call_duration=60)
    await run.start(ignore_register_failure=True)
    try:
        assert await _wait(lambda: run.controller.backoff_reason != "")
        await asyncio.sleep(0.5)
        st = run.controller.state()
        assert st["target"] <= 2 and st["established"] == 2
        assert run.stats.fail_by_status.get("486", 0) >= 3
    finally:
        await run.stop()


def test_prod_requires_confirmation():
    with pytest.raises(ProdConfirmationRequired):
        Run(scenario_from_dict(_scenario("prod")))
    run = Run(scenario_from_dict(_scenario("prod")), confirm_prod=True, target=40)
    assert run.config.max_concurrency == 20 and run.controller.target == 20
