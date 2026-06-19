# DESIGN.md — Closed-Loop Auto-Remediation (Ronki)

Tác giả: TienThanh · Decision engine: **Rule-based** (Option A)

Tài liệu này trả lời 4 câu hỏi bắt buộc của HANDOUT, kèm số liệu cụ thể.

---

## 1. Rule-based hay LLM-based? Vì sao? Trade-off?

**Chọn: Rule-based** (`runbook_map` trong `config.yaml`).

Ánh xạ cố định alertname → runbook:

| alertname | runbook | lý do |
|---|---|---|
| `HighLatency` | `restart_service.sh` | latency tăng vọt thường do process treo / GC pause → restart làm sạch trạng thái nhanh nhất |
| `HighErrorRate` | `clear_cache.sh` | lỗi 5xx hàng loạt thường do cache bẩn / config stale → flush cache (SIGHUP) rẻ và ít gây gián đoạn hơn restart |
| `InstanceDown` | `restart_service.sh` | service chết → phải bật lại container |
| `MultiStepDeploy` | `multi_step_deploy.sh` | deploy giao dịch nhiều bước có rollback ngược thứ tự |

**Vì sao rule-based:**
- **Tất định & kiểm toán được.** Cùng một alert luôn ra cùng một hành động — bắt buộc với automation chạm vào production. Không có "nhiệt độ", không bất ngờ.
- **Độ trễ ~0 và không phụ thuộc mạng ngoài.** Vòng điều khiển không bị chặn bởi một lời gọi API có thể timeout giữa lúc đang có sự cố.
- **Không gặp ảo giác (hallucination).** Vì đây chính là rủi ro mà acceptance test #6 nhắm tới.

**Trade-off (điểm yếu của rule-based):**
- Không khái quát được sự cố lạ ngoài bảng map → trả về `NO_RUNBOOK` và để con người xử lý (fail-safe, không đoán bừa).
- Phải bảo trì bảng map bằng tay khi thêm loại sự cố mới.

**Vì sao không LLM:** với không gian quyết định nhỏ (3–4 loại alert) và yêu cầu an toàn cao, một LLM thêm độ trễ, chi phí, phụ thuộc mạng và **rủi ro ảo giác** mà không đem lại lợi ích. Kiến trúc vẫn sẵn sàng cho LLM: lớp `validate_runbook()` chính là tấm chắn dùng chung cho cả rule-based lẫn LLM — nếu sau này nối LLM, chỉ cần để nó đề xuất `runbook` rồi đẩy qua đúng hàm validate này (reject nếu nằm ngoài `runbook_registry`, yêu cầu `confidence >= 0.6`).

---

## 2. Cấu hình blast-radius: giá trị & lý do

```yaml
blast_radius:
  max_actions_per_minute: 3
  max_restarts_per_service_per_hour: 5
```

- **`max_actions_per_minute: 3`** — toàn hệ thống chỉ có 5 service. Một cascade thật cùng lắm gây vài alert đồng thời; cho phép 3 hành động/phút đủ để xử lý song song 2–3 service mà **vẫn chặn được "automation storm"** (vòng lặp tự bắn hành động liên tục). Quá ngưỡng → `BLAST_RADIUS_EXCEEDED` → escalate cho người, không hành động.
- **`max_restarts_per_service_per_hour: 5`** — nếu **một** service phải restart >5 lần/giờ thì đó là lỗi gốc dai dẳng (DB hỏng, bug, đầy đĩa) mà restart không chữa được. Lúc đó cứ restart tiếp chỉ che giấu sự cố; nên dừng và để người điều tra.

Hai giới hạn **độc lập**: một cái chặn theo chiều rộng (đồng thời nhiều service), một cái chặn theo chiều sâu (lặp lại trên cùng một service). Cả hai dùng cửa sổ trượt (sliding window) trong `engine/safety.py`.

---

## 3. Verify check metric gì? Ngưỡng? Timeout?

Verify chọn metric **theo loại alert** (xem `engine/verify.py → ALERT_CHECKS`), cộng cổng liveness `up`:

| alert | metric kiểm tra | PromQL (rút gọn) | ngưỡng PASS |
|---|---|---|---|
| `HighLatency` | `latency_p99` (+`up`) | `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[1m]))*1000` | `< 500 ms` |
| `HighErrorRate` | `error_rate_pct` (+`up`) | `rate(http_errors_total[2m]) / rate(http_requests_total[2m]) *100` | `< 10 %` |
| `InstanceDown` | `up` | `up{job="<svc>"}` | `== 1` |

Tất cả ngưỡng lấy từ `data/baseline.json → verify_thresholds` (không hard-code trong Python):

```
latency_p99_max_ms = 500
error_rate_max_pct = 10.0
up_required        = 1
verify_timeout_seconds       = 60   ← timeout tổng
verify_poll_interval_seconds = 10   ← poll mỗi 10s
verify_min_samples           = 3    ← cần 3 mẫu PASS LIÊN TIẾP
```

- **Timeout = 60 s, poll mỗi 10 s** → tối đa 6 lần đo trong một cửa sổ verify.
- **Cần 3 mẫu PASS liên tiếp** (`min_samples`): một lần scrape may mắn không được tính là khỏi bệnh; phải khỏe ổn định qua ≥30s. Một mẫu fail làm reset bộ đếm về 0.

**Vì sao chọn metric theo alert thay vì kiểm tra mọi thứ:** service mock chỉ phát histogram latency khi có request. Sau `InstanceDown → restart`, service vừa lên có thể chưa có traffic → `latency_p99` trả `None`. Nếu cứ ép kiểm tra latency ở đây sẽ tạo **rollback giả**. Gắn kiểm tra đúng với triệu chứng đã báo động sẽ tránh được điều đó.

---

## 4. Circuit breaker reset khi nào? Tay hay tự động? Vì sao?

```yaml
circuit_breaker:
  consecutive_failure_threshold: 3
  reset_mode: manual
```

- **Mở (OPEN) khi:** 3 lần thất bại **liên tiếp** (action exec fail **hoặc** verify fail). Một lần thành công bất kỳ sẽ reset bộ đếm về 0 (`record_success`), nên ngưỡng là "3 liên tiếp", không phải "3 cộng dồn".
- **Reset: THỦ CÔNG** (khởi động lại tiến trình orchestrator).
- **Vì sao thủ công:** breaker mở nghĩa là remediation tự động đã thử 3 lần và đều thất bại → gần như chắc chắn lỗi gốc nằm ngoài tầm với của automation (hỏng tầng dưới, lỗi cấu hình, phụ thuộc ngoài). Tự reset sẽ tạo **vòng lặp flapping**: thử → fail → chờ → tự reset → thử lại mãi mãi, vừa che giấu sự cố vừa đốt blast-radius. Buộc con người can thiệp để vừa chẩn đoán đúng gốc rễ, vừa là điểm dừng an toàn cuối cùng (fail-safe). Hành vi này khớp `data/expected.json → scenario_3 (reset_mode: manual)`.

> Lưu ý an toàn (acceptance test #6): **thất bại do validation ≠ thất bại do hành động.** Khi `DECISION_VALIDATION_FAILED` xảy ra, code **không** gọi `record_failure()` → breaker không nhúc nhích. Một runbook bịa ra không bao giờ được phép làm hỏng cầu dao của các sự cố thật.
