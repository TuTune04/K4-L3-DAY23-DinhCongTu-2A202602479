# Postmortem — Region failover drill, Lab 23

## 1. Timeline

UTC times below follow the logs. User times are request starts; the first failed request returned after its 2046.2ms timeout.

| ISO time (UTC) | Event | Evidence |
|---|---|---|
| 2026-10-09T06:04:08 | Outage starts: region A suspended (netblock simulation) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T06:04:08.908496 | First affected user request starts | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T06:04:28 | Health-check alert: A UNHEALTHY after 3 failures | `reports/health-events.jsonl:2` |
| 2026-10-09T06:04:35 | Confirmed failover reaches cutover A → B; this records cutover, not the separate operator confirmation time | `reports/failover-events.jsonl:5` |
| 2026-10-09T06:04:37.308470 | Resolved: first successful request served by B | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO / RPO and gap analysis

- RTO target: 300s; measured: 28.5s; gap (remaining budget): 271.5s; PASS.
- RPO target: 300s; measured: 6.0s / 3 documents; gap: 294.0s; PASS. Restore evidence: `reports/failover-events.jsonl:2`.
- Detection was the slowest stage at 19.3s: a configured 15s floor plus polling phase and sequential probe timeouts. A suspended process accepts connections but cannot respond. Warm-up then required 6.29s (`reports/failover-events.jsonl:4`); observed edge cache delay was 1.947s.
- The breakdown includes a 5.263s residual for timeout, alert wait, restore and rounding; its rows sum to 28.500s. These are remaining-budget gaps, not target violations. Baseline had NO_RECOVERY within its observation window (`reports/drill-1-nodr.jsonl:32`).

## 3. Root cause — five whys

1. Why did customers see errors? Edge still routed to A while A could not answer requests.
2. Why did routing remain there? Cutover waited for a confirmed outage and a ready replacement, preventing unsafe automatic switching.
3. Why was the replacement not immediately ready? B was warm with no weights or vectors (`reports/failover-events.jsonl:1`), requiring restore and full-pool warm-up.
4. Why was restored data behind? Periodic snapshots leave a replication window; the restored latest document was 6.0s behind the primary and 3 documents were missing.
5. Why could a real outage defeat this procedure? Runbook step 3 depends on an accessible, compatible snapshot. Without one, restoration fails; additionally, the lab's RPO comparison reads A's local database, which may be unavailable in an actual permanent region loss. Independent replication telemetry and restore exercises are needed to validate that assumption.

This is a system dependency and preparedness issue, not an operator fault. The simulation preserves A's disk and does not prove resilience to loss of both A and the snapshot store.

## 4. Action items

Estimates are hypotheses to validate in another drill; overlapping savings should not be added blindly.

| Action item | Owner role | Deadline | Estimated seconds saved |
|---|---|---|---|
| Trial 1s health interval with 3-failure threshold, recovery hysteresis and IC-controlled cutover | On-call SRE | 2026-10-16 | About 12s RTO |
| Keep B full and prewarmed; continuously validate weights and vectors | ML platform | 2026-10-23 | About 6.29s RTO |
| Trial edge TTL 1s and verify cache propagation through edge requests | Edge platform SRE | 2026-10-23 | Up to about 1s RTO in this cache phase |
| Validate snapshot access/version independently of A; trial 2s replication and replay audit | Data platform | 2026-10-30 | About 4s RPO if lag falls from 6s to 2s |

## 5. Mandatory questions

1. interval × threshold = 5 × 3 = 15s, or 52.6% of the measured 28.5s RTO. Actual detection took 19.3s because polling and timeout overhead also matter.
2. At interval 1s the configured floor becomes 3s, reducing RTO by roughly 12s to 16.5s if other delays stay constant. More frequent probing increases load and sensitivity to short failures, false positives and flapping. Keep consecutive-failure checks, recovery hysteresis and incident commander confirmation; validate the estimate under netblock timeouts.
3. docs_lost = 3 means three primary documents are absent from the restored database at comparison time. If A is permanently lost for six hours and no independent replay source exists, customers cannot retrieve those documents through the recovered service. It is a count of documents, not customers or six hours of traffic. The live lab continued ingesting into A's local disk, so this restore-time metric must not be presented as a definitive count at outage start. Audit acknowledged writes and replay from durable upstream records where available.
