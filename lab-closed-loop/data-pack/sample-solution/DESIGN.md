# DESIGN.md — Ronki Closed-Loop Orchestrator

## 1. Decision engine: Rule-based hay LLM-based?

**Chọn: Rule-based.**

Lý do: Stack Ronki có 3 loại alert được định nghĩa rõ ràng (`HighLatency`, `HighErrorRate`, `InstanceDown`) và mỗi loại map 1-1 với một runbook đã được ops team kiểm chứng. Trong môi trường này, rule-based cho **latency quyết định < 1ms** và **deterministic — cùng alert luôn trigger cùng runbook**. LLM-based phù hợp hơn khi alert description phức tạp, ambiguous, hoặc khi cần reasoning qua nhiều bước như phân tích log + metric cùng lúc.

Trade-off:

| | Rule-based | LLM-based |
|---|---|---|
| Latency quyết định | < 1ms | 200–800ms (API round-trip) |
| Determinism | 100% | Phụ thuộc temperature, prompt |
| Mở rộng alert mới | Cần cập nhật map thủ công | Tự suy luận nếu prompt đủ tốt |
| Chi phí | Không | ~$0.002–0.01/quyết định |
| Fallback khi offline | Không cần | Cần rule-based fallback |

Kết luận: với 3 alert type cố định và yêu cầu reliability cao trong production lab, rule-based là lựa chọn đúng. Nếu mở rộng lên 20+ alert type với mô tả tự nhiên, sẽ xem xét LLM-based với confidence threshold 0.6.

## 2. Blast-radius config

```yaml
blast_radius:
  max_actions_per_minute: 3
  max_restarts_per_service_per_hour: 5
```

**Lý do chọn giá trị:**

- `max_actions_per_minute: 3` — stack có 5 service. Nếu cascade failure xảy ra, tối đa 3 action/phút tránh orchestrator restart đồng loạt tất cả service và làm tăng tải database. Con số này đủ phản ứng nhanh (3 service trong 1 phút) mà không gây thundering herd.
- `max_restarts_per_service_per_hour: 5` — nếu một service bị restart > 5 lần trong 1 giờ mà vẫn fail, đây là dấu hiệu của lỗi không tự phục hồi được (OOM liên tục, config sai, dependency down). Tiếp tục restart vô ích — cần human escalation.

Khi vượt ngưỡng: orchestrator log `BLAST_RADIUS_EXCEEDED` và không thực hiện action, để alert tiếp tục firing cho đến khi human can thiệp.

## 3. Verify step

**Metric kiểm tra:** p99 latency (ms) VÀ `up` (1/0).

**Threshold:**
- `latency_p99_max_ms: 500` — từ `baseline.json`, p99 bình thường dao động 72–230ms tùy service. Chọn 500ms = khoảng 2x baseline p99 của service chậm nhất (checkout-svc: 230ms), đủ rộng để tránh false negative nhưng vẫn phát hiện nếu action không có tác dụng.
- `up_required: 1` — service phải reachable trước khi verify latency có ý nghĩa.

**Timeout và polling:**
- `verify_timeout_seconds: 60` — restart container mất 5–10s, sau đó cần thêm 15–20s để metric ổn định trong Prometheus (scrape interval 10s). 60s = đủ thời gian cho 3 scrape cycle sau khi container up.
- `verify_poll_interval_seconds: 10` — match với scrape interval của Prometheus.
- `verify_min_samples: 3` — yêu cầu 3 sample liên tiếp đều pass trước khi kết luận verify thành công. Tránh false positive do một sample may mắn tốt.

## 4. Circuit breaker reset

**Reset mode: manual.**

Lý do: circuit breaker mở khi 3 consecutive failure xảy ra. Đây là trạng thái bất thường nghiêm trọng — orchestrator đã thử và thất bại 3 lần liên tiếp. Nếu tự động reset sau N phút, có nguy cơ orchestrator tiếp tục loop vô hạn và gây thêm disruption (thundering herd, database connection exhaustion, v.v.).

Manual reset đảm bảo: một kỹ sư xem xét log, xác định nguyên nhân gốc rễ, xác nhận fix xong trước khi automation tiếp tục. Chi phí của manual reset (vài phút delay) thấp hơn rủi ro của automated reset sai lúc.

Cách reset: `Ctrl+C` dừng orchestrator, fix issue, khởi động lại `uv run python closed_loop.py --config config.yaml`.

Nếu muốn automatic reset: thêm `cool_down_seconds: 1800` (30 phút) vào config và implement time-based reset. Nhưng phải có alert riêng để notify on-call khi circuit mở.

## 5. Mutex strategy (Stress 2 — concurrent alert race)

**Thiết kế**: một `threading.Lock` riêng biệt cho mỗi service name, lưu trong dict `_service_locks` bảo vệ bởi một meta-lock. Khi alert đến, orchestrator gọi `acquire(blocking=False)` — nếu service đang có runbook chạy thì log `SERVICE_LOCK_BUSY` và bỏ qua alert duplicate thay vì xếp hàng chờ. Hai service khác nhau luôn có lock khác nhau nên chạy song song không bị block.

Lý do dùng `blocking=False` thay vì queue: trong closed-loop production, một runbook đang chạy trên service A là sự kiện đang tiến hành — alert mới trên cùng service A trong vòng 30s là duplicate của cùng sự cố, không phải sự cố mới. Xếp hàng chờ sẽ gây re-execute runbook ngay sau khi lock release, tức là thực hiện action hai lần trên cùng service liên tiếp — nguy hiểm hơn là bỏ qua.

## 6. Rollback chain ordering (Stress 1 — multi-step transactional deploy)

**Thiết kế**: `run_transactional_steps` thực thi steps A→B→C và tích lũy danh sách `completed` theo thứ tự thực hiện. Khi step C fail, orchestrator lấy `rollback_steps[:len(completed)]` rồi duyệt `reversed()` — tức rollback-B trước rollback-A. Không rollback bước chưa bao giờ được thực thi.

Lý do reverse-order là đúng về mặt kỹ thuật: step A (drain traffic) tạo ra state mà step B (apply config) phụ thuộc vào. Nếu rollback A trước B, service có thể nhận traffic trong khi config đang ở trạng thái không nhất quán. Reverse order đảm bảo teardown đi ngược với setup — cùng nguyên lý LIFO stack như transaction rollback trong database.

## 7. Lý do chọn metrics cho observability

Năm metric được chọn theo nguyên tắc debug-driven: mỗi metric trả lời một câu hỏi cụ thể khi incident xảy ra. `closed_loop_actions_total{outcome}` cho biết ngay orchestrator đã act hay rollback — không cần đọc log. `closed_loop_circuit_breaker_state` hiện thị khi automation bị halt; nếu gauge = 1 mà không có alert nào được xử lý, kỹ sư biết ngay cần restart orchestrator thủ công. `closed_loop_blast_radius_remaining` cảnh báo sớm trước khi rate limit bị chạm — gauge về 0 nghĩa là orchestrator đang im lặng không phải vì không có alert mà vì đã dùng hết quota. `closed_loop_mutex_locked` giúp debug race condition: nếu một service liên tục LOCKED trong nhiều phút, runbook có thể bị treo. `closed_loop_verify_status` với giá trị in-progress (2) cho thấy orchestrator đang chờ Prometheus confirm — nếu trạng thái này kéo dài quá `verify_timeout_seconds`, verify đã timeout. Không có metric "vanity" như số lần poll hay số alert skipped — những con số đó không giúp tìm nguyên nhân gốc rễ nhanh hơn.

## 8. Decision validation policy (Stress 3 — LLM hallucination defense)

**Thiết kế**: trước khi gọi dry-run, `validate_runbook` kiểm tra tên runbook trả về từ decide engine có nằm trong `runbook_registry` (danh sách path được khai báo tường minh trong config) hay không. Nếu không có → log `DECISION_VALIDATION_FAILED` với đầy đủ `bad_runbook`, `alertname`, `raw_decision`, `action=escalate_no_auto_action`, rồi return ngay — không spawn subprocess, không thực thi gì.

Lý do cần whitelist tường minh: LLM có thể trả về tên runbook hợp lý về mặt ngôn ngữ nhưng không tồn tại trong hệ thống (`scale_down_database.sh`, `reboot_kernel.sh`). Nếu orchestrator tin tưởng tên đó và chạy `subprocess` với path không tồn tại, bash sẽ exit non-zero và action fail — nhưng failure đó sẽ increment circuit breaker counter, có thể mở circuit sau 3 lần hallucinate liên tiếp. Validation trước dry-run ngắt vòng lặp đó và giữ audit trail rõ ràng để human review.
