# SUBMIT.md — Closed-Loop Auto-Remediation

Sinh viên: Phạm Hữu Tiến Thành

> **Trạng thái: đã chạy thật cả 6 scenario trên Docker stack (2026-06-19).** Log thật
> được trích ở §3. Toàn bộ audit log gốc nằm trong `captured-logs/scen{1..6}.jsonl`
> (bản đầy đủ kèm cả các mẫu `VERIFY_SAMPLE`); ở đây chỉ trích chuỗi event chính cho
> dễ đọc — không sửa nội dung.

---

## 0. Cấu trúc bài nộp

```
submission-PhamHuuTienThanh/
├── closed_loop.py            # orchestrator (DETECT→DECIDE→DRY-RUN→ACT→VERIFY→ROLLBACK)
├── config.yaml               # runbook_map, registry, blast-radius, circuit-breaker, multi-step, ignore_services
├── engine/
│   ├── logger.py             # structured JSON log → stdout + AUDIT_LOG_PATH file
│   ├── metrics.py            # Prometheus metrics trên :9100
│   ├── safety.py             # BlastRadiusGuard + CircuitBreaker (thread-safe)
│   └── verify.py             # verify theo Prometheus, chọn metric theo loại alert
├── runbooks/
│   ├── restart_service.sh
│   ├── clear_cache.sh
│   ├── scale_replicas.sh
│   └── multi_step_deploy.sh  # deploy giao dịch + rollback ngược thứ tự (hỗ trợ FAIL_STEP)
├── tools/
│   ├── load_gen.sh           # tạo traffic để service phát metric
│   └── inject_synthetic_alert.sh   # bắn alert giả (cho scenario 4/5/6)
├── captured-logs/            # audit log thật của 6 scenario (đã chạy)
├── DESIGN.md
└── SUBMIT.md
```

---

## 1. Chuẩn bị môi trường

```bash
# Cài deps cho orchestrator
uv pip install requests pyyaml prometheus_client

# Dựng stack
bash ../data-pack/scripts/start_stack.sh

# Kiểm tra
curl -s http://localhost:9090/-/healthy   # Prometheus
curl -s http://localhost:9093/-/healthy   # Alertmanager

# Tạo traffic liên tục (terminal riêng) — BẮT BUỘC để có metric latency/error
bash tools/load_gen.sh
```

Chạy orchestrator (terminal riêng), bật ghi audit log cho Grafana/Loki:

```bash
AUDIT_LOG_PATH=audit_log.jsonl uv run python closed_loop.py --config config.yaml
```

---

## 2. Trạng thái đã kiểm chứng

| Hạng mục | Cách kiểm | Kết quả |
|---|---|---|
| `bash -n` toàn bộ runbooks/tools | syntax check | PASS |
| `py_compile` toàn bộ Python | compile | PASS |
| **Scenario 1** — Action success | chạy thật trên stack | PASS → `ACTION_SUCCESS` |
| **Scenario 2** — Action fail → rollback | chạy thật trên stack | PASS → `ROLLBACK_RESULT(still_unhealthy)` |
| **Scenario 3** — Circuit breaker | chạy thật trên stack | PASS → `CIRCUIT_BREAKER_HALT(=3)` |
| **Scenario 4** — Transactional rollback | chạy thật trên stack | PASS → rollback `[rollback-B, rollback-A]` |
| **Scenario 5** — Concurrent mutex | chạy thật trên stack | PASS → song song + `SERVICE_LOCK_BUSY` |
| **Scenario 6** — Hallucination defense | chạy thật trên stack | PASS → `DECISION_VALIDATION_FAILED`, breaker=0 |

### Lưu ý vận hành quan trọng (rút ra khi chạy thật)

1. **`baseline.json` chỉ đọc MỘT LẦN lúc orchestrator khởi động.** Khi cần ép verify
   fail cho scenario 2/3, phải sửa ngưỡng **rồi restart orchestrator** — sửa file lúc
   orchestrator đang chạy không có tác dụng.

2. **Ép verify fail cho `InstanceDown` = set `up_required: 2`, KHÔNG phải hạ `latency`.**
   Verify của `InstanceDown` chỉ kiểm `up` (xem `engine/verify.py::ALERT_CHECKS`), nên
   hạ `latency_p99_max_ms` vô tác dụng. Đặt `up_required: 2` (up tối đa = 1) → verify
   luôn fail → kích hoạt rollback/breaker đúng như mong muốn.

3. **macOS: lệnh `inject_fault.sh latency` cần `nsenter` (không có trên macOS) → bắn
   alert latency thật không được.** Thay bằng `tools/inject_synthetic_alert.sh HighLatency <svc>`
   POST thẳng alert vào Alertmanager. `kill`/`recover` (scenario 2/3/4) dùng `docker
   stop/start` nên chạy tốt trên macOS.

4. **Inhibition:** Alertmanager có rule `InstanceDown` → suppress `HighLatency|HighErrorRate`
   cùng service. Vì orchestrator lọc `inhibited=false`, khi test mutex cùng-service (scen 5b)
   phải dùng cặp alert KHÔNG có `InstanceDown` (vd `HighLatency` rồi `HighErrorRate`), nếu
   không alert thứ hai bị Alertmanager nuốt.

5. **Bug đã phát hiện & sửa khi chạy thật:** Prometheus scrape chính endpoint `:9100` của
   orchestrator (job `closed-loop`). Lúc vừa khởi động, `up==0` vài giây → `InstanceDown`
   về **chính orchestrator** → nó thử `docker restart ronki-closed-loop-orchestrator`
   (container không tồn tại) → `ACTION_EXEC_FAIL` nạp sai circuit breaker. Đã thêm
   `ignore_services: [closed-loop-orchestrator]` trong `config.yaml` + guard trong
   `process_alert` → giờ log `ALERT_IGNORED` thay vì tự remediate. Đã xác minh lại: khởi
   động sạch, breaker không bị nạp.

---

## 3. Sáu kịch bản nghiệm thu — LOG THẬT

### 3.1 Scenario 1 — Action success (latency → payment-svc)

```bash
# macOS: dùng synthetic (latency tc không chạy được)
bash tools/inject_synthetic_alert.sh HighLatency payment-svc
```
Log thật (`captured-logs/scen1.jsonl`):
```json
{"ts": "2026-06-19T04:43:44.797806+00:00", "event_type": "ALERT_DETECTED", "service": "payment-svc", "alertname": "HighLatency", "severity": "critical"}
{"ts": "2026-06-19T04:43:44.798694+00:00", "event_type": "DECIDE_RUNBOOK", "service": "payment-svc", "action": "runbooks/restart_service.sh", "alertname": "HighLatency"}
{"ts": "2026-06-19T04:43:44.798834+00:00", "event_type": "BLAST_RADIUS_OK", "service": "payment-svc", "remaining": 3}
{"ts": "2026-06-19T04:43:44.805818+00:00", "event_type": "DRY_RUN_PASS", "service": "payment-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T04:43:51.242749+00:00", "event_type": "ACTION_EXECUTED", "service": "payment-svc", "action": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T04:44:11.305860+00:00", "event_type": "VERIFY_PASS", "service": "payment-svc", "samples": 3}
{"ts": "2026-06-19T04:44:11.306405+00:00", "event_type": "ACTION_SUCCESS", "service": "payment-svc", "action": "runbooks/restart_service.sh", "result": "resolved", "alertname": "HighLatency"}
```
→ Đúng chuỗi `expected.json::scenario_1`. Verify cần 3 mẫu PASS liên tiếp (`samples: 3`).

---

### 3.2 Scenario 2 — Action fail → rollback (kill checkout-svc)

Ép verify fail: set `data/baseline.json → verify_thresholds.up_required = 2` rồi **restart
orchestrator**, sau đó:
```bash
bash ../data-pack/scripts/inject_fault.sh kill ronki-checkout-svc
```
Log thật (`captured-logs/scen2.jsonl`):
```json
{"ts": "2026-06-19T04:55:18.968215+00:00", "event_type": "ALERT_DETECTED", "service": "checkout-svc", "alertname": "InstanceDown", "severity": "critical"}
{"ts": "2026-06-19T04:55:18.968949+00:00", "event_type": "DECIDE_RUNBOOK", "service": "checkout-svc", "action": "runbooks/restart_service.sh", "alertname": "InstanceDown"}
{"ts": "2026-06-19T04:55:18.969061+00:00", "event_type": "BLAST_RADIUS_OK", "service": "checkout-svc", "remaining": 3}
{"ts": "2026-06-19T04:55:18.974964+00:00", "event_type": "DRY_RUN_PASS", "service": "checkout-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T04:55:24.194268+00:00", "event_type": "ACTION_EXECUTED", "service": "checkout-svc", "action": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T04:56:24.292883+00:00", "event_type": "VERIFY_FAIL", "service": "checkout-svc", "samples": 6}
{"ts": "2026-06-19T04:56:24.294867+00:00", "event_type": "ROLLBACK_TRIGGERED", "service": "checkout-svc", "action": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T04:56:30.714719+00:00", "event_type": "ROLLBACK_EXECUTED", "service": "checkout-svc", "action": "runbooks/restart_service.sh", "result": "ok"}
{"ts": "2026-06-19T04:57:30.827658+00:00", "event_type": "ROLLBACK_VERIFY_FAIL", "service": "checkout-svc", "samples": 6}
{"ts": "2026-06-19T04:57:30.830167+00:00", "event_type": "ROLLBACK_RESULT", "service": "checkout-svc", "result": "still_unhealthy"}
```
→ Verify fail (6 mẫu, không đạt 3 liên tiếp) → auto-rollback → **rollback được verify lại**
(`ROLLBACK_VERIFY_FAIL` → `ROLLBACK_RESULT`). failure_count = 1.

---

### 3.3 Scenario 3 — Circuit breaker (3 fail liên tiếp)

Giữ `up_required: 2`, lặp 3 lần kill→(xử lý+verify fail)→recover trên checkout-svc.
Log thật rút gọn (`captured-logs/scen3.jsonl`, đủ 98 dòng):
```json
{"ts": "2026-06-19T05:02:56.675376+00:00", "event_type": "ALERT_DETECTED", "service": "checkout-svc", "alertname": "InstanceDown"}
{"ts": "2026-06-19T05:04:02.043602+00:00", "event_type": "VERIFY_FAIL", "service": "checkout-svc", "samples": 6}
{"ts": "2026-06-19T05:04:02.044763+00:00", "event_type": "ROLLBACK_TRIGGERED", "service": "checkout-svc"}
{"ts": "2026-06-19T05:05:08.704768+00:00", "event_type": "ROLLBACK_RESULT", "service": "checkout-svc", "result": "still_unhealthy"}
{"ts": "2026-06-19T05:06:11.905907+00:00", "event_type": "ALERT_DETECTED", "service": "checkout-svc", "alertname": "InstanceDown"}
{"ts": "2026-06-19T05:07:17.283149+00:00", "event_type": "VERIFY_FAIL", "service": "checkout-svc", "samples": 6}
{"ts": "2026-06-19T05:07:17.284122+00:00", "event_type": "ROLLBACK_TRIGGERED", "service": "checkout-svc"}
{"ts": "2026-06-19T05:08:23.789668+00:00", "event_type": "ROLLBACK_RESULT", "service": "checkout-svc", "result": "still_unhealthy"}
{"ts": "2026-06-19T05:09:12.105223+00:00", "event_type": "ALERT_DETECTED", "service": "checkout-svc", "alertname": "InstanceDown"}
{"ts": "2026-06-19T05:10:17.434656+00:00", "event_type": "VERIFY_FAIL", "service": "checkout-svc", "samples": 6}
{"ts": "2026-06-19T05:10:17.435730+00:00", "event_type": "ROLLBACK_TRIGGERED", "service": "checkout-svc"}
{"ts": "2026-06-19T05:11:23.971063+00:00", "event_type": "ROLLBACK_RESULT", "service": "checkout-svc", "result": "still_unhealthy"}
{"ts": "2026-06-19T05:11:23.971474+00:00", "event_type": "CIRCUIT_BREAKER_HALT", "consecutive_failures": 3, "threshold": 3, "reset_mode": "manual", "message": "Automation halted. Restart the orchestrator to reset."}
{"ts": "2026-06-19T05:11:23.971834+00:00", "event_type": "CIRCUIT_OPEN", "service": "checkout-svc", "message": "breaker opened; automation halted"}
```
→ 3 cặp `VERIFY_FAIL + ROLLBACK_TRIGGERED` → `CIRCUIT_BREAKER_HALT(consecutive_failures=3)`
→ `CIRCUIT_OPEN` (polling dừng). Reset: Ctrl-C + chạy lại orchestrator.

---

### 3.4 Scenario 4 — Multi-step transactional rollback

Chạy orchestrator với `FAIL_STEP=C` (ép step-C exit 1 tất định) rồi bắn alert:
```bash
FAIL_STEP=C AUDIT_LOG_PATH=audit_log.jsonl uv run python closed_loop.py --config config.yaml
bash tools/inject_synthetic_alert.sh MultiStepDeploy api-gateway
```
Log thật (`captured-logs/scen4.jsonl`):
```json
{"ts": "2026-06-19T05:14:14.780557+00:00", "event_type": "ALERT_DETECTED", "service": "api-gateway", "alertname": "MultiStepDeploy"}
{"ts": "2026-06-19T05:14:14.781480+00:00", "event_type": "DECIDE_RUNBOOK", "service": "api-gateway", "action": "runbooks/multi_step_deploy.sh", "alertname": "MultiStepDeploy"}
{"ts": "2026-06-19T05:14:14.781684+00:00", "event_type": "BLAST_RADIUS_OK", "service": "api-gateway", "remaining": 3}
{"ts": "2026-06-19T05:14:14.789582+00:00", "event_type": "DRY_RUN_PASS", "service": "api-gateway", "runbook": "runbooks/multi_step_deploy.sh"}
{"ts": "2026-06-19T05:14:16.177691+00:00", "event_type": "TRANSACTIONAL_STEP", "service": "api-gateway", "result": "ok", "step": "step-A"}
{"ts": "2026-06-19T05:14:19.343809+00:00", "event_type": "TRANSACTIONAL_STEP", "service": "api-gateway", "result": "ok", "step": "step-B"}
{"ts": "2026-06-19T05:14:19.348251+00:00", "event_type": "TRANSACTIONAL_STEP_FAIL", "service": "api-gateway", "step": "step-C", "completed_before_failure": ["step-A", "step-B"]}
{"ts": "2026-06-19T05:14:19.348308+00:00", "event_type": "TRANSACTIONAL_ROLLBACK_STEP", "service": "api-gateway", "step": "rollback-B"}
{"ts": "2026-06-19T05:14:23.775282+00:00", "event_type": "TRANSACTIONAL_ROLLBACK_STEP", "service": "api-gateway", "step": "rollback-A"}
{"ts": "2026-06-19T05:14:25.819622+00:00", "event_type": "TRANSACTIONAL_ROLLBACK_COMPLETE", "service": "api-gateway", "rolled_back": ["rollback-B", "rollback-A"]}
```
→ step-A, step-B ok; step-C fail (`completed_before_failure=[step-A, step-B]`); rollback
đúng 2 bước, **ngược thứ tự** `[rollback-B, rollback-A]`. **Không có `ACTION_SUCCESS`.**

---

### 3.5 Scenario 5 — Concurrent alert race (mutex per-service)

```bash
# (a) 2 service khác nhau song song:
bash tools/inject_synthetic_alert.sh HighLatency checkout-svc &
bash tools/inject_synthetic_alert.sh HighLatency frontend &
wait
# (b) alert thứ 2 lên cùng checkout-svc khi runbook đầu đang chạy:
bash tools/inject_synthetic_alert.sh HighErrorRate checkout-svc
```
Log thật (`captured-logs/scen5.jsonl`):
```json
{"ts": "2026-06-19T05:46:00.183669+00:00", "event_type": "ALERT_DETECTED", "service": "frontend", "alertname": "HighLatency"}
{"ts": "2026-06-19T05:46:00.184424+00:00", "event_type": "ALERT_DETECTED", "service": "checkout-svc", "alertname": "HighLatency"}
{"ts": "2026-06-19T05:46:00.195872+00:00", "event_type": "DRY_RUN_PASS", "service": "frontend", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T05:46:00.196006+00:00", "event_type": "DRY_RUN_PASS", "service": "checkout-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T05:46:06.641559+00:00", "event_type": "ACTION_EXECUTED", "service": "frontend", "action": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T05:46:06.656517+00:00", "event_type": "ACTION_EXECUTED", "service": "checkout-svc", "action": "runbooks/restart_service.sh"}
{"ts": "2026-06-19T05:46:15.213102+00:00", "event_type": "ALERT_DETECTED", "service": "checkout-svc", "alertname": "HighErrorRate"}
{"ts": "2026-06-19T05:46:15.216139+00:00", "event_type": "SERVICE_LOCK_BUSY", "service": "checkout-svc", "message": "runbook already running for this service; skipping"}
{"ts": "2026-06-19T05:46:26.733341+00:00", "event_type": "ACTION_SUCCESS", "service": "checkout-svc", "action": "runbooks/restart_service.sh", "result": "resolved", "alertname": "HighLatency"}
{"ts": "2026-06-19T05:46:26.734645+00:00", "event_type": "ACTION_SUCCESS", "service": "frontend", "action": "runbooks/restart_service.sh", "result": "resolved", "alertname": "HighLatency"}
```
→ (a) 2 service `ALERT_DETECTED` cùng cycle (`.183`/`.184`), `DRY_RUN_PASS` lệch **1ms**
(`.195`/`.196`) → chạy song song, KHÔNG khóa nhau. (b) Alert thứ 2 trên checkout
(`05:46:15.213`) → `SERVICE_LOCK_BUSY` (`.216`) vì runbook HighLatency của checkout đang
giữ lock (giải phóng lúc `ACTION_SUCCESS 05:46:26`).

---

### 3.6 Scenario 6 — LLM hallucination defense

```bash
bash tools/inject_synthetic_alert.sh TestHallucination payment-svc
```
Log thật (`captured-logs/scen6.jsonl`):
```json
{"ts": "2026-06-19T05:47:15.291424+00:00", "event_type": "ALERT_DETECTED", "service": "payment-svc", "alertname": "TestHallucination", "severity": "critical"}
{"ts": "2026-06-19T05:47:15.291815+00:00", "event_type": "DECISION_VALIDATION_FAILED", "action": "escalate_no_auto_action", "bad_runbook": "runbooks/nonexistent_runbook.sh", "alertname": "TestHallucination", "raw_decision": "runbooks/nonexistent_runbook.sh"}
```
Khẳng định (đã kiểm chứng trên log thật):
- `DECISION_VALIDATION_FAILED` đủ 4 field: `bad_runbook`, `alertname`, `raw_decision`, `action`. ✅
- Trong audit log của scenario này: **0** dòng `DRY_RUN_PASS` / `ACTION_EXECUTED` / `RUNBOOK_EXEC` → không spawn subprocess. ✅
- Circuit breaker sau scenario: **0** (validation fail ≠ action fail). ✅

---

## 4. Bản đồ 5 sub-checkpoint → vị trí code

| Checkpoint | Vị trí |
|---|---|
| 1. Dry-run | `closed_loop.py::_process_locked` (gọi `run_runbook(..., dry_run=True)` trước tiên) |
| 2. Blast-radius | `engine/safety.py::BlastRadiusGuard`; gọi tại `process_alert` |
| 3. Verify (≥3 mẫu/60s, Prometheus) | `engine/verify.py::verify_service` |
| 4. Auto-rollback (+ verify lại) | `_process_locked` (`ROLLBACK_TRIGGERED` → `ROLLBACK_VERIFY_*`) |
| 5. Circuit breaker | `engine/safety.py::CircuitBreaker`; halt tại `main()` |
| Mutex per-service (stress) | `closed_loop.py::get_service_lock` + thread/alert |
| Decision validation (stress) | `closed_loop.py::validate_runbook` |
| Transactional rollback (stress) | `closed_loop.py::run_transactional` |
| Self-remediation guard | `closed_loop.py::process_alert` + `config.yaml::ignore_services` |

---

## 5. Ghi chú vận hành

- macOS: `inject_fault.sh latency` dùng `nsenter`/`tc` — không có trên macOS. Dùng
  `tools/inject_synthetic_alert.sh HighLatency <svc>` để bắn alert latency. `kill`/`recover`
  (scenario 2/3/4) luôn hoạt động.
- Sau khi test scenario 2/3, **trả `up_required` về 1** trong `data/baseline.json`.
- Đặt `AUDIT_LOG_PATH=audit_log.jsonl` để Promtail/Loki/Grafana đọc được audit tail.
- Metrics orchestrator: `curl -s http://localhost:9100/metrics | grep closed_loop`.
</content>
