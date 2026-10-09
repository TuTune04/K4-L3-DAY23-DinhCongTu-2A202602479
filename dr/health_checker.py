"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về readiness và lý do, luôn giới hạn thời gian request."""
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        if response.status_code == 200:
            return True, "ok"
        try:
            reasons = response.json().get("reasons", [])
        except ValueError:
            reasons = []
        return False, f"http_{response.status_code}:" + ",".join(reasons)
    except Exception as e:
        return False, type(e).__name__


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll hai region, chỉ ghi log khi trạng thái đổi sau đủ lần liên tiếp."""
    out.parent.mkdir(parents=True, exist_ok=True)
    regions = ["a", "b"]
    states = {region: "HEALTHY" for region in regions}
    fails = {region: 0 for region in regions}
    oks = {region: 0 for region in regions}
    start = time.time()
    while time.time() < start + duration:
        for region in regions:
            ready, reason = probe(region, timeout)
            ts = time.time()
            if ready:
                oks[region] += 1
                fails[region] = 0
            else:
                fails[region] += 1
                oks[region] = 0
            previous = states[region]
            if previous == "HEALTHY" and fails[region] >= threshold:
                states[region] = "UNHEALTHY"
            elif previous == "UNHEALTHY" and oks[region] >= threshold:
                states[region] = "HEALTHY"
            if states[region] != previous:
                event = {
                    "ts": ts,
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                    "event": "state_change",
                    "region": region,
                    "from": previous,
                    "to": states[region],
                    "reason": reason,
                    "consecutive_fails": fails[region],
                    "interval_s": interval,
                    "threshold": threshold,
                    "detect_floor_s": round(interval * threshold, 1),
                }
                line = json.dumps(event)
                with out.open("a") as log:
                    log.write(line + "\n")
                    log.flush()
                print(line)
        time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
