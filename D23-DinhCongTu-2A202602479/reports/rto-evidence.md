# RTO/RPO Evidence — Lab 23

All times are UTC. Offsets use request-start timestamps and are rounded to 0.1 seconds, matching the measurement tool.

## 1. Drill 1 — baseline without DR

| Metric | Value | Measurement | Evidence |
|---|---|---|---|
| t_outage | 2026-10-09T06:03:17 | Chaos kill of region A | `chaos/chaos-events.jsonl:1` |
| First failed request | +0.0s | First request with ok:false after outage | `reports/drill-1-nodr.jsonl:17` |
| Subsequent recovery | no recovery | No successful request after outage within the recorded window | `reports/drill-1-nodr.jsonl:32` |
| RTO | NO_RECOVERY | Observation ends with a failed request; not an infinite-duration claim | `reports/drill-1-nodr.jsonl:32` |

## 2. Drill 2 — with DR

| Milestone | Seconds from t_outage | Measurement | Evidence |
|---|---|---|---|
| t_outage | +0.0s | 2026-10-09T06:04:08, region A netblock | `chaos/chaos-events.jsonl:3` |
| First affected user | +0.1s | First ok:false request | `reports/drill-2-withdr.jsonl:25` |
| Health-check detection | +19.3s | UNHEALTHY region a, ReadTimeout | `reports/health-events.jsonl:2` |
| Snapshot restore complete | +20.2s | 2_restore_snapshot, ok:true | `reports/failover-events.jsonl:2` |
| Standby ready | +26.5s | 4_wait_ready, ok:true | `reports/failover-events.jsonl:4` |
| DNS cutover | +26.5s | 5_dns_cutover, a to b | `reports/failover-events.jsonl:5` |
| Measured recovery | +28.5s | First ok:true after failures, served_by:b | `reports/drill-2-withdr.jsonl:39` |

| Metric | Measured | Target | Verdict |
|---|---|---|---|
| RTO — Inference API | 28.5s | 300s | PASS |
| RPO — Vector DB | 6.0s / 3 doc | 300s | PASS |

RPO is the primary/restored latest-document timestamp difference at restore, not simply snapshot age: `reports/failover-events.jsonl:2`.

## 3. RTO breakdown

| Component | Seconds | Source | How to reduce |
|---|---|---|---|
| Health-check detect floor | 15.000s | 5s interval × 3 failures, `reports/health-events.jsonl:2` | Reduce interval to 1s with safeguards against flapping |
| Snapshot restore boundary | 0.000s | ts(3_scale_pool) − ts(2_restore_snapshot) = 0.000365s, rounded; `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:3` | Keep snapshots local and validate restoration regularly |
| GPU pool warm-up | 6.290s | waited_s, `reports/failover-events.jsonl:4` | Maintain a prewarmed full standby pool |
| DNS/LB TTL cache | 1.947s | Recovery ts − cutover ts = 1.946520s; `reports/drill-2-withdr.jsonl:39`, `reports/failover-events.jsonl:5` | Shorten edge TTL from 5s to 1s |
| Other / gap (alert wait, restore, probe timeout) | 5.263s | Residual: 28.500 − 15.000 − 0.000 − 6.290 − 1.947; `reports/health-events.jsonl:2`, `reports/failover-events.jsonl:1` | Bound probe timeouts and shorten alert-to-confirm latency |
| Total | 28.500s | Measured RTO rounded to 0.1s, `reports/drill-2-withdr.jsonl:39` | Prioritize detection and warm-up |

The residual includes rounding and snapshot work before the completion log. The restore boundary is not a measurement of the entire snapshot operation. Observed detection is 19.3s; the 15s configured floor is only one component. TTL delay depends on cache phase and is not always the full 5s setting.
