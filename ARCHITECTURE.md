# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Customer request | Điều phối công việc, xác định tool cần gọi ban đầu | Gọi Specialists hoặc tổng hợp kết quả |
| Order/item | `order_id` | Thu thập thông tin đơn hàng, sản phẩm (`get_order`, `get_order_items`) | Trả về thông tin chi tiết order/item |
| Payment | `order_id`, `payment_id` | Thu thập thông tin thanh toán, hoàn tiền (`get_order_payments`, `get_payment_timeline`, `get_refund_timeline`) | Trả về thông tin thanh toán |
| Shipment | `order_id`, `shipment_id` | Lấy dữ liệu vận chuyển (`get_shipment_summary`) | Trả về trạng thái giao hàng |
| Policy | `policy_version` | Lấy chính sách thương mại (`get_policy`) | Trả về quy định áp dụng |
| Verifier | Tất cả evidence | Đối chiếu các thông tin và schemas | JSON output chuẩn xác |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Mô tả message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Chỉ trace sự kiện/decision code quan sát được; không trace nội dung suy luận riêng.

## 4. Evidence lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Not found | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, evidence ownership, claim linkage, money totals, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và các giới hạn tài nguyên. Không ghi API key.
