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
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line)
    return record


def state_of(region) -> dict:
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    primary = "a" if target == "b" else "b"
    t0 = time.time()
    result = {"ok": False, "target": target, "rpo_seconds": None, "docs_lost": None}
    emit(step="1_verify_target", target=target, state=state_of(target))
    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as e:
        emit(step="2_restore_snapshot", ok=False, error=str(e))
        return {**result, "aborted_at": "2_restore_snapshot", "error": str(e),
                "elapsed_s": round(time.time() - t0, 2)}

    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    result.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
                  embed_model_version=meta.get("embed_model_version"))
    emit(step="2_restore_snapshot", ok=True, **r,
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"))

    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full")
    emit(step="3_scale_pool", pool_state="full")

    started = time.time()
    last_reason = "readiness not checked"
    while time.time() - started < wait:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=2)
            if response.status_code == 200:
                break
            last_reason = f"HTTP {response.status_code}"
        except Exception as e:
            last_reason = f"{type(e).__name__}: {e}"
        remaining = wait - (time.time() - started)
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    else:
        waited_s = round(time.time() - started, 2)
        emit(step="4_wait_ready", ok=False, waited_s=waited_s,
             last_reason=last_reason, action="ABORT_no_cutover")
        return {**result, "aborted_at": "4_wait_ready", "waited_s": waited_s,
                "last_reason": last_reason, "elapsed_s": round(time.time() - t0, 2)}

    waited_s = round(time.time() - started, 2)
    emit(step="4_wait_ready", ok=True, waited_s=waited_s)
    active = pathlib.Path("edge/active_region")
    old = active.read_text().strip() if active.exists() else "a"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(target)
    emit(step="5_dns_cutover", cutover_from=old, cutover_to=target)
    return {**result, "ok": True, "target_state": state_of(target),
            "cutover_from": old, "cutover_to": target, "waited_s": waited_s,
            "elapsed_s": round(time.time() - t0, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
