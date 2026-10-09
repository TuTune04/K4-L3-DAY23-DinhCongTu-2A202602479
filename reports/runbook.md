# Runbook — Primary region A unavailable

Run from the repository root in bare mode. Incident commander (IC) confirms recovery. Drill reference: RTO 28.5s, RPO 6.0s / 3 documents, warm-up 6.29s, edge TTL 5s.

| # | Step | Copy-paste commands | Done when | Owner |
|---|---|---|---|---|
| 1 | Confirm outage | `python3 chaos/kill_region.py status`<br>`python3 dr/health_checker.py --interval 5 --timeout 2 --threshold 3 --duration 300 --out reports/health-events.jsonl` | A has 3 consecutive readiness failures at 5s intervals and an UNHEALTHY alert; B alive=true. Run checker in another terminal. B may be unready before restore. | On-call SRE |
| 2 | Open incident and authorize | `date -u +%Y-%m-%dT%H:%M:%SZ`<br>`python3 dr/runbook.py --primary a --target b --backend fs` | Record outage/alert UTC in incident notes; IC confirms at prompt after detection. Result ok=true. This performs restore, scale, readiness and cutover once. | Incident commander / on-call SRE |
| 3 | Verify restored state | `curl -s --max-time 3 localhost:8002/v1/state` | weights=true, count ≥ 1, compatible model version, restore ok=true; RPO ≤ 300s (drill: 6.0s / 3 doc). Manual restore below if orchestration was not used. | Data platform |
| 4 | Verify pool readiness | `curl -s --max-time 3 -w '\nHTTP %{http_code}\n' localhost:8002/readyz` | HTTP 200 and full pool within 60s; drill warm-up took 6.29s. | ML platform |
| 5 | Verify cutover | `curl -s --max-time 3 localhost:8080/edge/state` | active_region=b after at most 5s TTL plus a probe; cutover follows readiness. | On-call SRE |
| 6 | Verify golden signals | `python3 loadgen/traffic.py --duration 60 --rps 2 --out /tmp/failover-verify.jsonl` | Calculate below: p95 < 500ms, errors < 1%, all requests served by B. Drill's 120 post-recovery requests had p95 47.1ms and 0% errors (drill-2 lines 39–158). | On-call SRE |
| 7 | Measure and close | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`<br>`python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300` | Drill 2 valid=true, warnings empty, RTO PASS ≤ 300s, RPO ≤ 300s; baseline NO_RECOVERY. Substitute actual incident traffic log for a new incident. Attach evidence and assign action items. | Incident commander |

Step 6 calculation:

```bash
python3 -c 'import json,math; r=[json.loads(x) for x in open("/tmp/failover-verify.jsonl")]; a=sorted(x["latency_ms"] for x in r); print({"p95_ms":a[math.ceil(.95*len(a))-1],"error_rate":sum(not x["ok"] for x in r)/len(r),"all_served_by_b":all(x.get("served_by")=="b" for x in r)})'
```

## Manual recovery path (steps 3–5)

Use only if orchestration has not completed recovery. Inspect failure logs and obtain IC confirmation. Never restore over B after it accepts writes. Abort on missing snapshot, incompatible model or failed restore.

```bash
python3 state/snapshot.py get --region b --backend fs
curl -s --max-time 3 localhost:8002/v1/state
echo full > state/region-b/pool_state
curl -s --max-time 3 -w '\nHTTP %{http_code}\n' localhost:8002/readyz
```

Repeat readiness until HTTP 200 (maximum 60s), verify weights=true and count ≥ 1. Only then:

```bash
printf b > edge/active_region
sleep 5
curl -s --max-time 3 localhost:8080/edge/state
```

## Rollback / fail back to A

IC decides, never automation. Require B degraded (p95 ≥ 500ms or error rate ≥ 1% over 60s), repaired A ready for at least 3 consecutive checks spaced 5s apart, and data re-synced from B. Freeze writes during synchronization; stop old A-to-store replication before publishing B's snapshot. Data platform verifies matching document counts/latest timestamps, compatible weights and no lost acknowledged writes. Keep B active during synchronization.

For this lab's SIGSTOP outage:

```bash
python3 chaos/kill_region.py restore --region a --backend bare
python3 state/snapshot.py put --region b --backend fs
python3 state/snapshot.py get --region a --backend fs
echo full > state/region-a/pool_state
curl -s --max-time 3 localhost:8002/v1/state
curl -s --max-time 3 localhost:8001/v1/state
```

After Data platform approves matching state, all three probes must return HTTP 200. Any failure means keep B active and investigate:

```bash
for i in 1 2 3; do
  curl -fsS --max-time 3 localhost:8001/readyz || break
  sleep 5
done
```

After all three successes and IC approval:

```bash
printf a > edge/active_region
sleep 5
curl -s --max-time 3 localhost:8080/edge/state
python3 loadgen/traffic.py --duration 60 --rps 2 --out /tmp/failback-verify.jsonl
python3 -c 'import json,math; r=[json.loads(x) for x in open("/tmp/failback-verify.jsonl")]; a=sorted(x["latency_ms"] for x in r); print({"p95_ms":a[math.ceil(.95*len(a))-1],"error_rate":sum(not x["ok"] for x in r)/len(r),"all_served_by_a":all(x.get("served_by")=="a" for x in r)})'
```

Done when edge active_region=a, all requests serve A, p95 < 500ms and errors < 1%. Resume writes and replication from the confirmed active region. If verification fails, IC may authorize `printf b > edge/active_region` after rechecking B readiness and data consistency; avoid automatic region flapping.
