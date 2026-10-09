# RTO/RPO Evidence — Lab 23

Tất cả số liệu dưới đây lấy từ drill của repo này. RTO được tính từ các timestamp loadgen; không lấy thời lượng ước đoán.

## 1. Drill 1 — Không có DR

| Chỉ số | Kết quả | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-10-09T04:30:17Z | Chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên | +0.3s | Request `ok:false` đầu tiên sau outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau outage | Không có | Không có request `ok:true` nào sau outage | `reports/measure-drill-1.json:25` |
| RTO | NO_RECOVERY | Đo từ loadgen và chaos log | `reports/measure-drill-1.json:25` |
| Lưu lượng | 32 request, 15 lỗi | Đếm request và kết quả measure | `reports/drill-1-nodr.jsonl:32`, `reports/measure-drill-1.json:28` |

## 2. Drill 2 — Có DR

| Mốc | Giây từ t_outage | Evidence |
|---|---:|---|
| t_outage (mốc 0) | 0.0s | `chaos/chaos-events.jsonl:5` |
| Người dùng thấy lỗi đầu tiên | +0.1s | `reports/drill-2-withdr.jsonl:26` |
| Health checker phát hiện A unhealthy | +14.1s | `reports/health-events.jsonl:5` |
| Restore snapshot hoàn tất | +14.5s | `reports/failover-events.jsonl:23` |
| Region B ready | +20.6s | `reports/failover-events.jsonl:25` |
| DNS cutover sang B | +20.6s | `reports/failover-events.jsonl:26` |
| Request thành công đầu tiên từ B; RTO | +22.7s | `reports/drill-2-withdr.jsonl:37` |

| Chỉ số | Đo được | Mục tiêu | Kết quả |
|---|---:|---:|---|
| RTO — Inference API | 22.7s | ≤300s | PASS |
| RPO — Vector DB | 28.0s / 14 tài liệu mất | ≤300s | PASS |

## 3. RTO breakdown

Các khoảng thời gian được chia tại các milestone tương ứng; các thành phần cộng lại thành RTO đã làm tròn 22.7s.

| Thành phần | Giây | Nguồn tính | Evidence |
|---|---:|---|---|
| Health-check detection | 14.13s (floor cấu hình 15s) | t_detect − t_outage; interval 5s × threshold 3 | `reports/health-events.jsonl:5` |
| Snapshot restore | 0.36s | Event restore − t_detect | `reports/failover-events.jsonl:23`, `reports/health-events.jsonl:5` |
| GPU pool warm-up | 6.13s | Region B ready − event restore; warm-up quan sát 6.11s | `reports/failover-events.jsonl:23`, `reports/failover-events.jsonl:25` |
| DNS/LB TTL cache | 2.05s | Request phục hồi đầu tiên − DNS cutover | `reports/failover-events.jsonl:26`, `reports/drill-2-withdr.jsonl:37` |
| **Tổng** | **22.67s ≈ 22.7s** | So với RTO tính từ timestamp loadgen | `reports/drill-2-withdr.jsonl:37` |

RPO đo khi restore là 28.0 giây và 14 document không có trong replica; model version được ghi nhận là `embed-model=vi-e5-base@v3` tại `reports/failover-events.jsonl:23`.
