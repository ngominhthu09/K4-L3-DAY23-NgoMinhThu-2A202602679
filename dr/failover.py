"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import datetime
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped JSONL event and print it for the operator."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    rec = {
        "ts": ts,
        "iso": datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
    print(json.dumps(rec, ensure_ascii=False))
    return rec


def state_of(region: str) -> dict:
    """Read the serving state of one region."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, warm, and cut traffic over only after the target is ready."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    primary = "b" if target == "a" else "a"
    result = {"ok": False, "target": target, "primary": primary}

    # 1. Record the target's starting condition. A not-ready target is expected:
    # the following restore and scale steps are responsible for making it ready.
    try:
        before = state_of(target)
        emit(step="1_verify_target", region=target, ok=True, state=before)
        result["target_before"] = before
    except Exception as exc:
        emit(step="1_verify_target", region=target, ok=False,
             error=type(exc).__name__, detail=str(exc))
        result["error"] = f"target state check failed: {type(exc).__name__}: {exc}"
        return result

    # 2. Restore vector data and model weights, then measure actual data loss
    # against the other region (the primary for this failover direction).
    try:
        meta = snapshot.get(target, backend)
        rpo_info = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        restore_event = emit(
            step="2_restore_snapshot",
            region=target,
            ok=True,
            snapshot_at=meta.get("snapshot_at"),
            restored_at=meta.get("restored_at"),
            rpo_seconds=rpo_info.get("rpo_seconds"),
            docs_lost=rpo_info.get("docs_lost"),
            embed_model_version=meta.get("embed_model_version"),
            latest_doc_ts=meta.get("latest_doc_ts"),
        )
        result.update({"snapshot": meta, "rpo": rpo_info,
                       "restore_event": restore_event})
    except (Exception, SystemExit) as exc:
        emit(step="2_restore_snapshot", region=target, ok=False,
             error=type(exc).__name__, detail=str(exc))
        result["error"] = f"snapshot restore failed: {type(exc).__name__}: {exc}"
        return result

    # 3. Ask the serving process to start its warm-up by changing pool state
    # while it is running.
    try:
        pool_file = pathlib.Path(f"state/region-{target}/pool_state")
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        previous_pool_state = pool_file.read_text().strip() if pool_file.exists() else None
        pool_file.write_text("full\n", encoding="utf-8")
        emit(step="3_scale_pool", region=target, ok=True,
             previous_pool_state=previous_pool_state, pool_state="full")
    except Exception as exc:
        emit(step="3_scale_pool", region=target, ok=False,
             error=type(exc).__name__, detail=str(exc))
        result["error"] = f"pool scale failed: {type(exc).__name__}: {exc}"
        return result

    # 4. Readiness includes pool warm-up, model weights, and non-empty vectors.
    # Keep polling through transient HTTP/network errors until the deadline.
    started = time.monotonic()
    deadline = started + max(0.0, wait)
    ready = False
    last_reason = "ready check not attempted"
    last_status = None
    while True:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=1.0)
            last_status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {}
            ready = response.status_code == 200 and body.get("ready", True)
            if ready:
                last_reason = "ready"
                break
            reasons = body.get("reasons", [])
            last_reason = ",".join(str(item) for item in reasons) or f"http_{response.status_code}"
        except Exception as exc:
            last_status = None
            last_reason = type(exc).__name__

        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(0.25, deadline - now))

    waited_s = round(time.monotonic() - started, 2)
    emit(step="4_wait_ready", region=target, ok=ready, ready=ready,
         waited_s=waited_s, status=last_status, reason=last_reason)
    if not ready:
        result.update({"error": f"target did not become ready within {wait}s: {last_reason}",
                       "waited_s": waited_s, "ready": False})
        return result

    target_state = {
        "region": target,
        "pool_state": body.get("pool_state"),
        "weights": True,
        **body.get("vectors", {}),
    }

    # 5. Cut over only after a successful readiness response.
    try:
        active_file = pathlib.Path("edge/active_region")
        active_file.parent.mkdir(parents=True, exist_ok=True)
        previous_region = active_file.read_text().strip() if active_file.exists() else primary
        active_file.write_text(f"{target}\n", encoding="utf-8")
        cutover = emit(step="5_dns_cutover", region=target, ok=True,
                       previous_region=previous_region, active_region=target)
        result.update({"ok": True, "ready": True, "waited_s": waited_s,
                       "active_region": target, "target_state": target_state,
                       "cutover": cutover})
    except Exception as exc:
        emit(step="5_dns_cutover", region=target, ok=False,
             error=type(exc).__name__, detail=str(exc))
        result["error"] = f"DNS cutover failed: {type(exc).__name__}: {exc}"
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
