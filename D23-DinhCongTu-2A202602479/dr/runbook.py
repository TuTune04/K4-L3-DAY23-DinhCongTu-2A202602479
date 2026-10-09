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
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, /, **kw):
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record)
    with LOG.open("a") as log:
        log.write(line + "\n")
    print(line)
    return record


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def events(path):
    """Read complete JSONL records while another process may be appending."""
    try:
        lines = pathlib.Path(path).read_text().splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def alive(region, endpoint):
    try:
        return httpx.get(f"{URL[region]}/{endpoint}", timeout=2).status_code == 200
    except Exception:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    start = time.time()
    kills = [e for e in events("chaos/chaos-events.jsonl")
             if e.get("action") == "kill" and e.get("region") == primary]
    t_outage = kills[-1].get("ts") if kills else None
    earliest = max(start - 600, t_outage or 0)
    t_detect = None
    source = "health_checker"
    deadline = start + 90
    while True:
        alerts = [e for e in events("reports/health-events.jsonl")
                  if e.get("event") == "state_change"
                  and e.get("region") == primary and e.get("to") == "UNHEALTHY"
                  and isinstance(e.get("ts"), (int, float))
                  and e["ts"] >= earliest]
        if alerts:
            t_detect = alerts[-1]["ts"]
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(1, remaining))

    if t_detect is None:
        source = "runbook_probe"
        failures = []
        for i in range(3):
            failed = not alive(primary, "readyz")
            failures.append(failed)
            if failed:
                t_detect = time.time()
            if i < 2:
                time.sleep(5)
        if not all(failures):
            t_detect = None
    confirmed = t_detect is not None
    step(1, "xac_nhan_outage", confirmed=confirmed, t_detect=t_detect,
         source=source, target_alive=alive(target, "healthz"))
    if not confirmed:
        return {"ok": False, "reason": "outage_not_confirmed"}

    t_announce = time.time()
    notify_delay_s = round(t_announce - t_outage, 2) if t_outage is not None else None
    step(2, "thong_bao_incident", t_outage=t_outage,
         t_outage_iso=(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_outage))
                       if t_outage is not None else None),
         t_detect=t_detect, t_announce=t_announce, notify_delay_s=notify_delay_s)
    if not confirm(auto, f"Cutover {primary} -> {target}?"):
        step(2, "aborted_by_operator")
        return {"ok": False}

    res = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=res["ok"], elapsed=res.get("elapsed_s"))
    if not res["ok"]:
        return {"ok": False, "t_detect": t_detect, "notify_delay_s": notify_delay_s,
                "failover": res}

    state = res["target_state"]
    step(4, "verify_state_replica", count=state.get("count"),
         weights=state.get("weights"), pool_state=state.get("pool_state"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"))
    step(5, "dns_cutover", ok=res["ok"], cutover_from=res.get("cutover_from"),
         cutover_to=res.get("cutover_to"))

    latencies = []
    errors = 0
    for _ in range(10):
        request_start = time.perf_counter()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3)
            errors += response.status_code != 200
        except Exception:
            errors += 1
        latencies.append((time.perf_counter() - request_start) * 1000)
    ordered = sorted(latencies)
    golden = {"n": 10, "p50_ms": round((ordered[4] + ordered[5]) / 2, 2),
              "p95_ms": round(ordered[9], 2), "error_rate": errors / 10,
              "latencies_ms": [round(ms, 2) for ms in latencies]}
    step(6, "verify_golden_signals", **golden)
    elapsed_s = round(time.time() - start, 2)
    step(7, "post_incident", elapsed_s=elapsed_s,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {"ok": res["ok"], "t_detect": t_detect, "notify_delay_s": notify_delay_s,
            "failover": res, "golden_signals": golden, "elapsed_s": elapsed_s}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
