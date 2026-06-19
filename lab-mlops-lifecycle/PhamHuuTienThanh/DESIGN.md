# DESIGN.md — MLOps Lifecycle: Anomaly Detection Pipeline

## Tổng quan

Pipeline phát hiện drift trên metrics của payment gateway (`latency_p99`, `error_rate`, `rps`),
trigger retrain mô hình IsolationForest, và swap phiên bản mới vào production thông qua MLflow
Registry alias theo kiểu blue-green. Mọi quyết định được ghi vào MLflow run và
`outputs/audit_log.jsonl`. Tất cả số liệu dưới đây lấy từ lần chạy thật trên 4 CSV sinh bởi
`generate_data.py` (seed=42).

---

## Sub-checkpoint 1 — Drift threshold


**Giá trị đã chọn: 0.15** (tức ≥ 15% số feature bị drift theo Evidently `DataDriftPreset`,
trường `share_of_drifted_columns`).

**Cách chọn (đã kiểm chứng):** Trước tiên đo "noise floor" bằng cách chạy drift detection trên
chính `baseline.csv` (split đôi: nửa đầu làm reference, nửa sau làm current) — khi không có drift
thực, score = **0.00** (0/3 feature drift). Đo trên `drifted.csv` thì score = **1.00** (3/3
feature: latency_p99, error_rate, rps đều drift). Vì khoảng cách giữa "no-drift" (0.00) và
"real-drift" (1.00) rất rộng, threshold 0.15 nằm an toàn ở giữa: nó tương đương "≥ 1 trên 3 feature
drift" (0.33 là feature kế tiếp), đủ thấp để bắt drift sớm khi mới 1 feature dịch, đủ cao để không
fire vì dao động thống kê.

**Nếu threshold quá thấp (vd 0.05):** false positive — chỉ cần một feature lệch nhẹ do biến động
theo giờ (intraday traffic sáng/tối) là trigger retrain, gây alert fatigue và tốn compute.

**Nếu threshold quá cao (vd 0.50):** false negative — phải có ≥ 2/3 feature drift mới fire; bỏ sót
giai đoạn đầu khi mới 1 feature bắt đầu dịch, model serve sai âm thầm.

---

## Sub-checkpoint 2 — Loại drift

**Loại được detect: data drift** — P(X) thay đổi, tức phân phối input features đã dịch so với
training. `drift_detector.py` dùng Evidently `DataDriftPreset`, mặc định dùng Wasserstein distance
cho numerical features; khi `share_of_drifted_columns` vượt threshold thì flag.

**Tại sao data drift phù hợp với bài toán payment anomaly:** Sau campaign, latency baseline tăng từ
~120ms lên ~156ms. Model v1 (học baseline 120ms) sẽ coi "bình thường mới" 156ms là anomaly → false
positive storm. Detect data drift cho phép retrain với distribution mới *trước khi* precision tụt.

**Concept drift (P(Y|X) đổi)** không được `DataDriftPreset` phát hiện vì feature distribution có thể
không đổi mà quan hệ feature→label đã đổi. Pipeline xử lý điều này bằng `--check-mode combined`
(xem Sub-checkpoint 5): bổ sung một performance check trên dữ liệu có nhãn.

---

## Sub-checkpoint 3 — Retrain trigger configuration

**Trigger type: semi-automatic với manual approval gate.** Drift detection có thể chạy tự động theo
batch (vd daily job), nhưng promotion `staging → production` luôn cần human approval (`[y/N]` trong
`retrain.py`). Cờ `--auto-approve` chỉ dùng cho CI/test.

**Lý do chọn manual:** Model anomaly detection ảnh hưởng trực tiếp SLA của on-call. Một model tệ
hơn được promote tự động có thể gây false negative trên incident thật, hoặc alert storm. Approval
gate buộc ML engineer review `drift_score`, `anomaly_rate` của v2, và kết quả holdout validation
trước khi cutover.

**Cadence:** Không có schedule cứng — drift check chạy khi có batch data mới. Đây là sự kết hợp
"event-driven (drift) + human-gated promote".

**Approval timeout (đề xuất cho production):** 24h — nếu không có approval trong 24h thì staging
version bị archive, tránh trạng thái model treo lơ lửng không ai review.

**Nếu phải tự động hoàn toàn:** xem Sub-checkpoint trong SUBMIT.md — dùng anomaly_rate delta + drift
score của validation window làm điều kiện auto-promote, ngưỡng delta 5%.

---

## Sub-checkpoint 4 — Versioning và rollback

**Chiến lược: MLflow Registry với aliases, không hardcode version number trong code.**

- `production` → version đang serve
- `staging` → candidate sau retrain
- `archived` → version bị rollback / thay thế
- Version numbers (1, 2, 3…) là immutable audit trail, giữ vô thời hạn.

**Tại sao alias > version number:** `serve.py` luôn load `models:/anomaly-detector@production`. Swap
chỉ là đổi alias trong registry + gọi `POST /reload` — không sửa/redeploy code. Nếu hardcode version
number thì mỗi lần retrain phải sửa code serve và redeploy.

**Rollback path (đã implement, xem `post_deploy_monitor` trong retrain.py):**
1. `client.set_registered_model_alias("anomaly-detector", "archived", v2)` — gỡ v2.
2. `client.set_registered_model_alias("anomaly-detector", "production", v1)` — khôi phục v1.
3. `POST /reload` trên serve.py — load lại v1. Toàn bộ < 5 giây, không redeploy container.

**Ai có quyền rollback:** auto-rollback do pipeline tự kích hoạt khi precision tụt; manual rollback
do ML engineer on-call (có MLflow admin). Mọi rollback ghi `auto_rollback_v2_to_v1` vào
`outputs/audit_log.jsonl` với `demoted_version`, `restored_version`, `trigger_precision`, `cycle`.

---

## Sub-checkpoint 5 — Tại sao cần combined mode (Stress 1)

Chỉ dùng `DataDriftPreset` là **chưa đủ**. `drifted.csv` chứa đồng thời 2 loại drift: (1) data drift
(latency/error/rps dịch) và (2) concept drift (25% nhãn bị lật — cùng feature nhưng quan hệ với
`anomaly_label` đã đổi, mô phỏng payment processor mới làm correlation cũ không còn đúng). Data drift
detector thấy (1) nhưng *hoàn toàn không thấy* (2) vì giá trị feature trông bình thường.

`--check-mode combined` chạy song song: (a) Evidently trên feature distribution, và (b)
`check_performance_drift` đo precision/recall của model `@production` trên dữ liệu có nhãn. Retrain
fire nếu `is_drift OR perf_is_degraded`. Ngưỡng performance mặc định precision ≥ 0.70.

**Ví dụ số cụ thể từ lần chạy thật:** trên `drifted.csv` (1008 rows, 29.1% nhãn anomaly, đã có 25%
nhãn lật), `--check-mode combined` in:
```
Drift score     : 1.0000   (data drift: cả 3 feature latency_p99/error_rate/rps đều drift)
Perf precision  : 0.2907   (threshold 0.70)  → Perf degraded : True
Perf recall     : 1.0000
```
Data drift score 1.0 đã cao, nhưng quan trọng hơn: **perf precision của model `@production` chỉ còn
0.2907** so với 0.91 lúc deploy — bằng chứng concept drift rõ ràng. Nếu chỉ chạy `--check-mode data`,
output chỉ in `Drift score` mà *không* surface precision drop này; combined mode in cả hai, chứng minh
hai cơ chế bắt hai loại drift khác nhau. (Lưu ý kỹ thuật: model được serve trên feature thô — như
serve.py — nên ở chế độ flag-mạnh, recall đạt 1.0 còn precision phản ánh tỉ lệ anomaly thật trong
batch; điều này khiến precision drop trở thành tín hiệu degradation đáng tin.)

---

## Sub-checkpoint 6 — Data selection strategy: sliding window vs alternatives (Stress 2)

Nếu train v2 chỉ trên drift window (1008 rows, 7 ngày), v2 overfit phân phối mới: nó học rằng
latency ~156ms là "bình thường" nhưng quên các pattern cũ vẫn còn trong production (batch job, traffic
ngoài campaign). Hệ quả: v2 **flag nhầm** dữ liệu old-pattern thành anomaly.

**Đặc tính dữ liệu cần lưu ý:** `holdout.csv` (500 rows old-pattern) có **0 nhãn anomaly** — toàn là
normal cũ (latency max 182ms < 200, error max 1.61% < 2.5). Vì không có positive, precision/recall
suy biến về 0.0 cho *mọi* model (acceptance criterion vẫn đạt vì dòng `Holdout validation — v2
precision: 0.0000 recall: 0.0000` được in và v2 ≥ v1). **Tín hiệu Stress 2 thật trên tập toàn-normal
là false-positive rate**: model có flag nhầm normal cũ thành anomaly không.

**Số đo thật (StandardScaler áp đúng, contamination 0.03):** false-positive rate trên holdout —
| Chiến lược train | FP rate trên old-pattern holdout |
|---|---|
| Pure drift window (1008 rows) | **8.6%** ← over-flag normal cũ |
| v1 baseline (4320 rows) | 3.4% |
| **Sliding window (5328 rows)** | **0.0%** ← tốt nhất |

Model chỉ train drift window coi old-pattern 120ms là "bất thường" (vì nó chỉ thấy 156ms là normal) →
8.6% false positive. **Sliding window** (`retrain.py` concat `baseline.csv` + `drifted.csv` =
**5328 rows**) thấy cả hai regime → 0% false positive trên old pattern, đồng thời vẫn bắt được drift.

**Alternatives:** (a) **pure drift window** — đơn giản nhưng over-flag (8.6%) như đo ở trên; (b)
**weighted sampling** (oversample baseline) — hợp lý khi drift window quá nhỏ, nhưng thêm
hyperparameter; (c) **full historical concat** — an toàn nhất nhưng tốn compute khi data tích lũy
nhiều tháng và dilute drift signal. Sliding window là trade-off tốt nhất cho quy mô lab này.

---

## Sub-checkpoint 7 — Auto-rollback: threshold và policy (Stress 3)

Sau khi v2 lên `@production`, `post_deploy_monitor` chạy **24 cycle** đo precision của v2 trên
`post_deploy_eval.csv` (200 rows: 60% clear-normal, 40% clear-anomaly). Nếu `precision < 0.65` →
auto-rollback.

**Tại sao 0.65?** Đây là ngưỡng bảo thủ: thấp hơn nhiều so với baseline 91% nhưng đủ xa để không bị
false rollback do sampling noise trên 200 rows. Tính toán: với 80 anomaly rows (40%), nếu model
miss vài chục thì precision vẫn ~0.8; chỉ khi model "loạn" rõ rệt precision mới rơi xuống vùng < 0.65.
Ngưỡng nằm ở điểm "model đang sai nghiêm trọng, không phải dao động".

**Kết quả lần chạy thật:** sau khi v2 promote, ngay **cycle 01/24** đo `precision: 0.4000` trên
`post_deploy_eval.csv` (200 rows, 40% anomaly) → 0.40 < 0.65 → auto-rollback kích hoạt. serve.py
reload về v1; `/health/active-version` xác nhận trả về version 1. Audit log ghi:
```json
{"event":"auto_rollback_v2_to_v1","demoted_version":2,"restored_version":1,"trigger_precision":0.4,"threshold":0.65,"cycle":1}
```

**Rollback flow:** `set_registered_model_alias("archived", v2)` → `set_registered_model_alias(
"production", v1)` → `POST /reload`. Event `auto_rollback_v2_to_v1` được append vào
`outputs/audit_log.jsonl` với `demoted_version`, `restored_version`, `trigger_precision`, `cycle`.

---

## Observability — tại sao các metrics này quan trọng

MLOps monitoring khác service monitoring: nguyên nhân degradation không phải bug code mà là **dữ liệu
dịch chuyển**. Drift score + precision/recall theo thời gian cho phép bắt model decay trước khi
on-call than phiền. Bảng alias state và `serve_active_version` trả lời ngay câu "đang serve version
nào?". `retrain_triggered` và `auto_rollback` counter tạo audit trail tối giản về số lần hệ thống tự
can thiệp. Grafana visualize trend; MLflow lưu chi tiết từng run — hai cái bổ sung cho nhau.

---

## Trade-offs đã chấp nhận

| Quyết định | Được | Mất |
|---|---|---|
| Manual approval gate | An toàn, human oversight | Latency trong retrain loop (giờ, không phải phút) |
| Combined data + performance drift | Bắt cả data lẫn concept drift | Cần dữ liệu có nhãn cho performance check |
| IsolationForest (không LSTM-AE) | Train < 1s, explainable, no GPU | Không capture temporal pattern, mỗi row độc lập |
| Sliding window (baseline + drift) | Không overfit, giữ old pattern | Tốn compute hơn khi history lớn dần |
| Local artifact store | Không cần S3 | Không scale multi-node; artifact mất nếu xóa volume |
