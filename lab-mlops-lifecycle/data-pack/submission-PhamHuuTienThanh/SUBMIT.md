# SUBMIT.md — Reflection: MLOps Lifecycle Lab

## Câu 1: Drift threshold bạn chọn là bao nhiêu và tại sao? Có validate trên dữ liệu thật không?

Threshold = **0.15** trên `share_of_drifted_columns` của Evidently. Tôi validate bằng cách đo "noise
floor": chạy drift detection trên chính `baseline.csv` (split nửa đầu/nửa sau) → score = **0.00**
(không feature nào drift). Chạy trên `drifted.csv` → score = **1.00** (cả 3 feature latency_p99,
error_rate, rps đều drift). Vì hai cực cách xa nhau, 0.15 (≈ "1/3 feature drift") nằm an toàn ở giữa:
đủ thấp để bắt drift khi mới 1 feature dịch, đủ cao để không fire vì biến động theo giờ. Nếu chọn
0.05 sẽ false positive theo intraday pattern; chọn 0.50 sẽ bỏ sót giai đoạn drift sớm.

---

## Câu 2: Nếu model v2 sau retrain lại tệ hơn v1 trong production thì sao? Pipeline xử lý thế nào?

Có **2 lớp bảo vệ**. (1) *Trước promote*: `retrain.py` validate v2 trên `holdout.csv` (old pattern,
có nhãn) và in `v2 precision/recall` cạnh `v1 precision/recall` — nếu v2 kém hơn, ML engineer từ chối
ở approval gate, v2 ở lại `staging`. (2) *Sau promote*: `post_deploy_monitor` đo precision của v2 trên
`post_deploy_eval.csv` suốt 24 cycle; nếu `precision < 0.65`, pipeline **tự rollback**: đặt v2 →
`@archived`, khôi phục v1 → `@production`, gọi `POST /reload`. Toàn bộ < 5 giây vì chỉ đổi alias,
không redeploy. Sự kiện ghi vào `outputs/audit_log.jsonl` (`auto_rollback_v2_to_v1`).

---

## Câu 3: Khác biệt giữa data drift và concept drift? Evidently detect loại nào trong lab này?

**Data drift**: P(X) đổi, quan hệ X→Y giữ nguyên — vd latency baseline tăng 120ms→156ms sau khi thêm
3rd-party integration. **Concept drift**: P(Y|X) đổi — cùng input nhưng nhãn đúng đã khác, vd
payment processor mới khiến cùng mức error_rate trước là anomaly nay là bình thường. Evidently
`DataDriftPreset` chỉ detect **data drift** (test thống kê trên feature distribution). Concept drift
trong `drifted.csv` (25% nhãn bị lật) Evidently không thấy được — tôi phát hiện nó bằng performance
check (`--check-mode combined`): đo precision của model `@production` trên dữ liệu có nhãn, precision
tụt xuống **0.2907** (so với 0.91 lúc deploy) là tín hiệu concept drift dù data drift score đã = 1.0.

---

## Câu 4: Tại sao blue-green swap quan trọng hơn replace file model trực tiếp?

Replace file trực tiếp gây race condition: serve.py đang đọc model cũ trong khi file bị ghi đè →
corrupted read / wrong prediction, và không còn đường lùi vì version cũ đã bị xóa. Blue-green qua
MLflow alias: alias `production` được swap atomically v1→v2 trong registry; serve.py chỉ load model
mới khi nhận `POST /reload` nên các in-flight request hoàn tất với model cũ. Nếu v2 lỗi, swap alias
về v1 + reload = rollback tức thì, **cả hai version vẫn tồn tại** trong registry — không mất gì,
không redeploy. `/health/active-version` cho phép verify đang serve version nào trước/sau cutover.

---

## Câu 5: Nếu phải tự động hoá approval gate (không cần người), dùng metric và threshold nào?

Tôi sẽ dùng **anomaly_rate delta** + **holdout precision** làm cổng tự động, với 3 điều kiện đồng
thời: (a) `v2_holdout_precision ≥ v1_holdout_precision` (không regress trên old pattern — đã có sẵn
trong pipeline); (b) `abs(v2_anomaly_rate − v1_anomaly_rate) < 0.05` (behavior không lệch quá nhiều);
(c) `0.01 < v2_anomaly_rate < 0.10` (không degenerate: không flag tất cả, cũng không bỏ sót tất cả).
Ngưỡng delta 5% là bảo thủ cho payment domain — 5% trên 1000 req/phút ≈ 50 cảnh báo sai/phút. Nếu cả
3 thỏa thì auto-promote; nếu không, giữ ở staging và đẩy alert cho engineer review trong 4h. Quan
trọng: vẫn giữ `post_deploy_monitor` + auto-rollback làm lưới an toàn cuối, vì gate tự động dù tốt
vẫn có thể sai trên distribution chưa từng thấy.
