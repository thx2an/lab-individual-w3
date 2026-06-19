# SUBMIT.md — Reflection: MLOps Lifecycle Lab

## Câu 1: Drift threshold bạn chọn là bao nhiêu và tại sao?

Threshold là **0.15** (15% features drifted). Cách chọn: chạy drift_detector trên chính baseline.csv, split 70/30 — noise floor đo được 0.04. Threshold 0.15 = 3.75× noise floor, đủ xa để không bị false positive từ seasonal variation (sáng/tối), nhưng đủ thấp để catch drift thực. Khi test với drifted.csv, score = 0.67 — vượt threshold rõ ràng. Nếu chọn 0.05, drift check sẽ fire mỗi ngày do intraday traffic pattern. Nếu chọn 0.50, sẽ bỏ sót drift giai đoạn đầu khi chỉ 1-2 features bắt đầu dịch.

---

## Câu 2: Điều gì xảy ra nếu model v2 sau retrain lại tệ hơn v1?

Pipeline hiện tại có manual approval gate — ML engineer xem anomaly_rate của v2 trước khi promote. Nếu v2 anomaly_rate bất thường (quá cao hoặc quá thấp so với v1), engineer từ chối promote, v2 ở lại alias `staging`. Rollback nếu v2 đã được promote: gọi `MlflowClient.set_registered_model_alias("anomaly-detector", "production", "1")` để swap alias về v1, sau đó `POST /reload` trên serve.py. Toàn bộ < 30 giây vì chỉ thay đổi alias, không redeploy. Cải tiến: implement shadow mode để so sánh v1 vs v2 song song trên production traffic trước khi cutover.

---

## Câu 3: Sự khác biệt giữa data drift và concept drift?

**Data drift**: phân phối input thay đổi — P(X) thay đổi, nhưng mối quan hệ X→Y giữ nguyên. Ví dụ: latency baseline tăng từ 120ms lên 156ms vì thêm 3rd-party integration. Model vẫn đúng về nguyên tắc nhưng threshold anomaly không còn phù hợp.

**Concept drift**: mối quan hệ input-output thay đổi — P(Y|X) thay đổi. Ví dụ: cùng latency 200ms trước đây là anomaly, nhưng sau khi scale up infra thì 200ms là bình thường. Model hoàn toàn sai dù input distribution không đổi nhiều.

Evidently DataDriftPreset trong lab này detect **data drift** bằng statistical test trên feature values. Concept drift không được detect trực tiếp vì không có production labels. Proxy: monitor anomaly_rate trend qua thời gian trong MLflow — nếu rate tăng đột biến mà không có real incident, đó là dấu hiệu concept drift.

---

## Câu 4: Tại sao blue-green swap quan trọng hơn replace file trực tiếp?

Replace file trực tiếp (overwrite model artifact) tạo ra race condition: serve.py đang xử lý request dùng model cũ, đồng thời file bị ghi đè → corrupted read → crash hoặc wrong prediction. Không có rollback — version cũ đã bị xóa.

Blue-green qua MLflow alias: alias `production` được swap atomically từ v1 → v2. Serve.py chỉ load model mới khi nhận `POST /reload` — tất cả in-flight request trước đó hoàn thành với v1. Nếu v2 có vấn đề, swap alias về v1 + reload = rollback ngay lập tức mà không cần redeploy. Cả 2 versions tồn tại song song trong registry — không mất gì.

---

## Câu 5: Nếu automate approval gate, dùng metric gì và threshold nào?

Dùng **anomaly_rate delta** giữa v2 và v1 trên cùng một validation window (dùng 20% cuối của current window làm holdout). Điều kiện auto-promote:

- `abs(v2_anomaly_rate - v1_anomaly_rate) < 0.05` — v2 không thay đổi behavior quá nhiều
- `v2_anomaly_rate < 0.10` — không bị degenerate (flag toàn bộ data là anomaly)
- `v2_anomaly_rate > 0.01` — không bị quá conservative (không phát hiện gì)

Ngưỡng 5% delta là conservative cho payment domain — sai lệch 5% trên 1000 requests/phút = 50 missed anomalies/phút, chưa kể SLA impact. Ngoài ra cần kiểm tra drift score của v2 validation window < threshold (tức v2 train trên distribution đúng). Nếu cả 3 điều kiện thỏa, auto-promote. Nếu không, đẩy alert cho ML engineer review trong 4h.
