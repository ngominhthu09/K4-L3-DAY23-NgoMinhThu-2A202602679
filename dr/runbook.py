"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import datetime
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append a timestamped runbook timeline event."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    rec = {
        "ts": ts,
        "iso": datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Automatically confirm only when explicitly requested; otherwise prompt."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step semi-automated regional failover runbook."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions: a and b")

    run_started = time.time()
    result = {"ok": False, "primary": primary, "target": target}

    # 1. Require repeated readiness failures on the primary and verify that the
    # target process is alive before proceeding.
    from dr import health_checker

    consecutive_failures = 0
    last_reason = "unknown"
    for attempt in range(1, 4):
        ready, last_reason = health_checker.probe(primary, timeout=0.75)
        if ready:
            consecutive_failures = 0
            break
        consecutive_failures += 1
        if attempt < 3:
            time.sleep(0.3)

    target_alive = False
    target_alive_error = None
    try:
        response = httpx.get(f"{URL[target]}/healthz", timeout=1.5)
        target_alive = response.status_code == 200
    except Exception as exc:
        target_alive_error = type(exc).__name__

    outage_confirmed = consecutive_failures >= 3 and target_alive
    outage_event = step(
        1, "xac_nhan_outage", primary=primary, target=target,
        outage_confirmed=outage_confirmed,
        primary_consecutive_failures=consecutive_failures,
        primary_reason=last_reason,
        target_alive=target_alive,
        target_error=target_alive_error,
    )
    result["outage_check"] = outage_event
    if not outage_confirmed:
        result["error"] = "outage not confirmed or target process is not alive"
        return result

    # 2. Record both the actual chaos timestamp and when the operator learned
    # about the incident. The runbook timestamp must be later than the outage.
    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    outage_ts = None
    if chaos_path.exists():
        for line in chaos_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("action") == "kill" and event.get("region") == primary:
                outage_ts = event.get("ts")
    incident_ts = time.time()
    incident_event = step(
        2, "thong_bao_incident", incident_ts=incident_ts,
        t_outage=outage_ts,
        notification_delay_s=(round(incident_ts - outage_ts, 2)
                              if outage_ts is not None else None),
        confirmed_by="auto" if auto else "operator",
    )
    result["incident"] = incident_event

    if not confirm(auto, f"Failover {primary} -> {target} đã được xác nhận. Tiếp tục?"):
        result["error"] = "operator declined failover"
        step(3, "scale_gpu_pool", primary=primary, target=target,
             started=False, reason="operator_declined")
        return result

    # Wait for the independent health checker to record the outage before
    # acting. This keeps the failover timeline evidence grounded in detection,
    # and prevents cutover from preceding t_detect in the measured drill.
    health_log = pathlib.Path("reports/health-events.jsonl")
    detection_started = time.monotonic()
    detection_deadline = detection_started + 60.0
    detected_event = None
    outage_floor = outage_ts if outage_ts is not None else incident_ts
    while time.monotonic() < detection_deadline:
        if health_log.exists():
            for line in health_log.read_text(encoding="utf-8").splitlines():
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (candidate.get("event") == "state_change"
                        and candidate.get("to") == "UNHEALTHY"
                        and candidate.get("region") == primary
                        and candidate.get("ts", 0) >= outage_floor):
                    detected_event = candidate
                    break
        if detected_event:
            break
        time.sleep(0.25)

    if detected_event is None:
        result["error"] = "health checker did not confirm the primary outage within 60s"
        step(3, "scale_gpu_pool", primary=primary, target=target,
             started=False, reason="health_detection_timeout",
             health_log=str(health_log))
        return result

    # 3. This is the only failover invocation in the runbook.
    failover_result = fo.failover(target, backend, wait=60)
    failover_event = step(
        3, "scale_gpu_pool", primary=primary, target=target,
        started=True, failover_ok=bool(failover_result.get("ok")),
        health_detected_ts=detected_event["ts"],
        health_detection_wait_s=round(time.monotonic() - detection_started, 2),
        failover_result=failover_result,
    )
    result["failover"] = failover_result
    result["scale_event"] = failover_event
    if not failover_result.get("ok"):
        result["error"] = failover_result.get("error", "failover failed")
        return result

    # 4. Verify the restored replica by reading state; do not run failover again.
    try:
        target_state = failover_result.get("target_state", {})
        replica_ok = bool(target_state.get("weights")) and target_state.get("count", 0) > 0
        replica_event = step(
            4, "verify_state_replica", ok=replica_ok,
            region=target, count=target_state.get("count"),
            weights=target_state.get("weights"),
            pool_state=target_state.get("pool_state"),
            rpo=failover_result.get("rpo"),
        )
        result["target_state"] = target_state
        result["replica_event"] = replica_event
        if not replica_ok:
            result["error"] = "restored replica is missing weights or vectors"
            return result
    except Exception as exc:
        step(4, "verify_state_replica", ok=False,
             region=target, error=type(exc).__name__, detail=str(exc))
        result["error"] = f"replica verification failed: {type(exc).__name__}: {exc}"
        return result

    # 5. Read back the cutover state; do not perform another cutover here.
    active_file = pathlib.Path("edge/active_region")
    active_region = active_file.read_text().strip() if active_file.exists() else None
    cutover_ok = active_region == target
    result["cutover_event"] = step(
        5, "dns_cutover", ok=cutover_ok,
        expected_region=target, active_region=active_region,
    )
    if not cutover_ok:
        result["error"] = "edge active region does not match failover target"
        return result

    # 6. Send ten real requests directly to the target region and capture the
    # error rate and p95 latency for the incident record.
    latencies = []
    failures = 0
    for i in range(10):
        started = time.perf_counter()
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"runbook golden signal {i}"},
                timeout=3.0,
            )
            body = response.json()
            ok = response.status_code == 200 and body.get("region") == target
        except Exception:
            ok = False
        latencies.append((time.perf_counter() - started) * 1000)
        failures += 0 if ok else 1

    p95_ms = round(sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1], 1)
    golden = {
        "requests": 10,
        "failures": failures,
        "error_rate": round(failures / 10, 3),
        "p95_latency_ms": p95_ms,
        "latencies_ms": [round(value, 1) for value in latencies],
    }
    result["golden_signals"] = golden
    result["golden_event"] = step(
        6, "verify_golden_signals", region=target, **golden,
    )

    # 7. The load generator may still be running, so record the measurement
    # command for the operator to run after it finishes.
    elapsed_s = round(time.time() - incident_ts, 2)
    result["ok"] = failures == 0
    result["post_incident_event"] = step(
        7, "post_incident", ok=result["ok"], elapsed_s=elapsed_s,
        rto_command=("python3 tools/measure_rto.py --loadgen "
                     "reports/drill-2-withdr.jsonl --target-rto 300"),
        measurement_pending=True,
    )
    if failures:
        result["error"] = f"golden signal check had {failures}/10 failed requests"
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
