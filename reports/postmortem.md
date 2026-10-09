# Postmortem — DR Drill Lab 23

Ngày drill: 2026-10-09. Đây là phân tích blameless về độ trễ và các kiểm soát cần duy trì.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09 04:53:26.396Z | Region A bị netblock; RTO clock bắt đầu. | `chaos/chaos-events.jsonl:5` |
| 2026-10-09 04:53:26.534Z | Request đầu tiên thất bại. | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09 04:53:30.622Z | Runbook ghi nhận operator đã mở incident. | `reports/runbook-run.jsonl:9` |
| 2026-10-09 04:53:40.529Z | Health checker đánh dấu A `UNHEALTHY` sau 3 lần lỗi liên tiếp. | `reports/health-events.jsonl:5` |
| 2026-10-09 04:53:40.885Z | Restore snapshot B hoàn tất; RPO 28.0s, mất 14 tài liệu. | `reports/failover-events.jsonl:23` |
| 2026-10-09 04:53:47.014Z | B trả `/readyz` 200 sau warm-up 6.11s. | `reports/failover-events.jsonl:25` |
| 2026-10-09 04:53:47.028Z | Edge cutover sang B. | `reports/failover-events.jsonl:26` |
| 2026-10-09 04:53:49.077Z | Request đầu tiên thành công từ B; sự cố được khôi phục. | `reports/drill-2-withdr.jsonl:37` |

## 2. RTO/RPO và gap so với mục tiêu

- RTO mục tiêu: 300s; đo được: 22.7s; còn 277.3s dưới ngưỡng.
- RPO mục tiêu: 300s; đo được: 28.0s (14 tài liệu không có trong replica); còn 272.0s dưới ngưỡng.
- Bước tốn nhiều thời gian nhất: health-check detection, 14.13s, khoảng 62% RTO. Đây là khoảng chờ polling và ngưỡng chống flapping.
- RTO breakdown: detection 14.13s + restore 0.36s + warm-up 6.13s + DNS/LB cache 2.05s = 22.67s, làm tròn thành 22.7s.

## 3. Root cause — 5 Whys

1. Vì sao inference trả lỗi? Edge vẫn trỏ tới Region A sau khi A bị netblock.
2. Vì sao edge chưa đổi ngay? Bài lab không có cơ chế DR tự phát hiện outage và điều phối cutover trước khi health checker hoàn tất ngưỡng.
3. Vì sao cần chờ health checker? Một probe lỗi đơn lẻ có thể là nhiễu; cần 3 lỗi liên tiếp để hạn chế flapping.
4. Vì sao không thể chuyển ngay sang B? B bắt đầu thiếu vector DB và model weights; nó phải restore snapshot và warm pool trước khi ready.
5. Vì sao các bước này cần runbook? Detection, state replication, readiness, và DNS/LB là các lớp độc lập; không có một tín hiệu duy nhất chứng minh toàn vùng đã phục vụ an toàn.

## 4. Action items

| # | Action | Owner | Deadline | Ước tính tác động |
|---|---|---|---|---|
| 1 | Giữ cảnh báo readiness độc lập với serving process và diễn tập runbook hàng tháng. | SRE on-call | 2026-10-16 | Phát hiện sớm hơn trong vận hành; không thay đổi RPO. |
| 2 | Thử interval 2s với threshold 3 trong staging; theo dõi false positive trước khi đổi cấu hình. | SRE | 2026-10-23 | Detection floor lý thuyết giảm từ 15s xuống 6s; có thể giảm RTO tối đa khoảng 9s, đổi lại nhiều probe hơn và rủi ro flapping cao hơn. |
| 3 | Giảm chu kỳ replication từ 30s xuống 10s nếu chi phí I/O chấp nhận được; xác nhận bằng nhiều lần đo. | ML platform | 2026-10-23 | RPO lý thuyết giảm tối đa khoảng 20s; cần đo lại docs lost. |

## 5. Câu hỏi bắt buộc

1. `interval × threshold` = 5s × 3 = 15s; lần chạy đo được detection 14.13s, khoảng 62% RTO.
2. Hạ interval xuống 1s làm detection floor lý thuyết giảm từ 15s xuống 3s, tiết kiệm tối đa 12s; chi phí là probe thường xuyên hơn và tăng khả năng false positive/flapping.
3. Nếu outage kéo dài 6 giờ và primary mất dữ liệu vĩnh viễn, `docs_lost = 14` nghĩa là 14 tài liệu đã được xác nhận trong primary nhưng replica tại thời điểm restore không có. Khách hàng cần biết chính xác phạm vi dữ liệu thiếu để đối soát hoặc gửi lại.
