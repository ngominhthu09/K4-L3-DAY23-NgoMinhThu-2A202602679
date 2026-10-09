# Runbook 1 trang — Region chính down

Phạm vi: sự cố Region A, failover sang Region B trong môi trường local WSL. Dùng `fs` backend. Người trực sự cố (on-call) xác nhận trước khi chuyển vùng; chỉ dùng `--auto` trong drill/CI.

| # | Bước | Lệnh | Xác nhận hoàn tất | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage và vùng dự phòng còn sống | `python3 chaos/kill_region.py status` | A lỗi readiness qua 3 lần probe; B còn sống. Runbook cũng tự xác nhận điều kiện này. | On-call |
| 2 | Mở incident, ghi mốc bắt đầu RTO | `date -u +%FT%TZ` | Thời điểm operator nhận tin được ghi vào `reports/runbook-run.jsonl`; t_outage lấy từ `chaos/chaos-events.jsonl`. | Incident commander |
| 3 | Khôi phục snapshot, warm pool, cutover | `python3 dr/runbook.py --primary a --target b --backend fs` | Xác nhận `y` khi được hỏi. Runbook chờ health checker báo A `UNHEALTHY`, đợi B `/readyz` trả 200 rồi mới cutover. | Incident commander + on-call |
| 4 | Xác minh replica tại B | `curl -fsS http://127.0.0.1:8002/v1/state` | `weights: true`, `count > 0`, `pool_state: full`; RPO và docs lost có trong `reports/runbook-run.jsonl`. | ML platform on-call |
| 5 | Xác minh edge đã chuyển sang B | `curl -fsS http://127.0.0.1:8080/edge/state` | `active_region` là `b`. | On-call |
| 6 | Kiểm tra golden signals | `python3 -c 'import httpx; rs=[httpx.get("http://127.0.0.1:8002/v1/infer", timeout=3) for _ in range(10)]; print("success", sum(r.status_code==200 for r in rs), "/ 10")'` | 10 request thành công; runbook ghi error rate và p95 latency vào `reports/runbook-run.jsonl`. | Service owner |
| 7 | Đo RTO/RPO và lưu bằng chứng | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300 > reports/measure-drill-2.json` | Mở file JSON và xác nhận `valid: true`, `warnings: []`, `rto_verdict: PASS`; chạy sau khi loadgen kết thúc. | Incident commander |

## Rollback / failback

Chỉ Incident Commander được quyết định failback về A sau khi A đã được khôi phục, `/readyz` trả 200 liên tiếp 3 lần, dữ liệu phát sinh ở B đã được đối soát/replicate, và các golden signals ổn định. Không failback tự động giữa hai vùng vì có thể gây flapping.

```bash
python3 chaos/kill_region.py restore --region a --backend bare
for i in 1 2 3; do curl -fsS http://127.0.0.1:8001/readyz >/dev/null || exit 1; sleep 5; done
printf a > edge/active_region
```

Rollback ngay nếu B không ready trong 60 giây hoặc golden signals có lỗi; giữ traffic ở vùng đang phục vụ tốt nếu A chưa qua readiness gate. Không trỏ traffic về A chỉ vì B gặp lỗi.
