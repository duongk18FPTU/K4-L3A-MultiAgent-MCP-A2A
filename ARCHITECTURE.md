# L3A Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Hệ thống không ghi
prompt bí mật hoặc chain-of-thought vào trace.

## 1. System overview

```text
Input → Coordinator ──task_assigned──┬→ Order/Item Agent ─┐
                                    ├→ Payment Agent ────┤
                                    ├→ Shipment Agent ───┼→ Coordinator
                                    └→ Policy Agent ─────┘       │
                                              │                  handoff
                                              └── MCP Gateway     │
                                                                 ▼
                                                        Verifier Agent
                                                                 │
                                                                 ▼
                                                        Contract-valid Output
```

Coordinator coi topic trong customer request là giả thuyết định tuyến, không phải ground
truth. Specialist lấy evidence có thẩm quyền; coordinator chỉ kết luận khi điều kiện nghiệp
vụ được xác nhận trong evidence và có policy tương ứng. Mọi bước quan sát được đều đi qua
`TraceWriter`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool được phép | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case công khai | Discovery, lập kế hoạch, fan-out/fan-in, tổng hợp | Không gọi evidence tool trực tiếp | Task và bản nháp kết luận |
| Order/Item | `case_id`, `order_id` | Xác minh trạng thái đơn, chọn item cùng timeline | `get_order`, `get_order_items` | Order/item evidence và entity IDs |
| Payment | `case_id`, `order_id` | Kiểm tra capture, split, mismatch, duplicate và refund | `get_payment_timeline`, `get_refund_timeline` | Payment/refund evidence |
| Shipment | `case_id`, `order_id` | Kiểm tra thời điểm giao và actor gây trễ | `get_shipment_summary` | Shipment evidence |
| Policy | `case_id`, `policy_version` | Chọn rule, status, action, refund và trách nhiệm | `get_policy` | Policy decision |
| Verifier | Output nháp + refs | Kiểm tra invariants trước khi trả output | Không có | `VERIFIED` hoặc `NEEDS_INVESTIGATION` |

Permission được khóa bằng allow-list trong `workflow.py`. Một tool chỉ được gọi nếu vừa
nằm trong allow-list của actor, vừa xuất hiện trong kết quả MCP discovery.

## 3. A2A protocol

Mỗi handoff nội bộ mang `case_id`, actor nguồn/đích, decision code và danh sách
`evidence_ref`. `case_id` của lời gọi MCP luôn lấy từ case đang xử lý; không nhận từ payload
của specialist. Coordinator tạo một plan hữu hạn, mỗi specialist chạy tối đa một lần cho
mỗi case rồi handoff về coordinator, do đó không có vòng lặp A2A.

Các specialist độc lập chạy đồng thời bằng `asyncio.gather`, với semaphore giới hạn hai MCP
call cùng lúc để bảo vệ stream. Mỗi MCP call được retry tối đa một lần sau 250 ms. Trace chỉ
chứa event, decision code, số lượng lỗi và evidence refs; không chứa suy luận riêng.

## 4. Evidence lifecycle

1. Gateway discovery xác nhận tên tool trước khi gọi.
2. `EvidenceGateway` validate envelope bằng `mcp-evidence-response-v1.schema.json`.
3. Workflow kiểm tra response thuộc đúng `order_id` hoặc `policy_version` của case.
4. `evidence_ref` được giữ nguyên, chỉ lưu trong scope của lần gọi `solve_case`.
5. Khi data được dùng cho entity, claim hoặc kết luận, agent emit
   `tool_result_consumed` với đúng tool và ref.
6. Các record trùng ID được chọn theo timeline gần `order_approved_at`; phần mâu thuẫn được
   công bố trong `data_conflicts`, không bị âm thầm bỏ qua.
7. Claim chỉ nhận các refs tham gia trực tiếp vào việc xác minh claim và policy.

Không có cache evidence dùng chung giữa case. Tool response sai entity scope bị loại bỏ và
không được emit như evidence đã sử dụng.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/runtime error | Một lần, 250 ms | Bỏ evidence lỗi; kết quả `insufficient_evidence` | `handoff/PARTIAL_EVIDENCE` |
| Tool không có trong discovery | Không | Không gọi; yêu cầu evidence có thẩm quyền | `handoff/PARTIAL_EVIDENCE` |
| Response sai entity scope/schema | Schema: gateway từ chối; scope: không retry | Không tiêu thụ ref | `handoff/PARTIAL_EVIDENCE` |
| Source conflict | Không | Chọn record cùng timeline và ghi `data_conflicts` | `verification_completed` |
| Policy thiếu hoặc không khớp | Một lần nếu call lỗi | Refund bằng 0, `needs_investigation` | `policy_decided/POLICY_UNAVAILABLE` |
| Invalid output invariant | Không | Dừng case bằng `ValueError`, không ghi output sai | Không finalize |

Workflow không chuyển missing evidence thành dữ liệu phỏng đoán và không tự tạo
`evidence_ref`.

## 6. Verification invariants

Trước finalize, verifier kiểm tra:

- `case_id` output khớp tuyệt đối với input;
- evidence refs là duy nhất và đều đến từ MCP response đã tiêu thụ trong case;
- order/item/event được giới hạn trong entity và timeline của case;
- tổng `refund_lines` bằng `recommended_refund_brl`;
- responsibility, action và refund lấy cùng một policy rule;
- confidence nằm trong `[0, 1]`;
- output cuối tiếp tục được CLI validate bằng `l3a-output-v2.schema.json` trước khi ghi file.

## 7. Reproducibility

- Runtime: Python 3.11+ và dependency ranges trong `pyproject.toml`.
- Framework: Python async state machine; không phụ thuộc model sinh nội dung.
- Concurrency: tối đa 2 MCP call trong một case; các case chạy tuần tự.
- Retry: một lần/call, backoff cố định 250 ms.
- Random seed: không dùng. Chỉ `event_id` của trace là ngẫu nhiên và không ảnh hưởng kết quả.
- Lệnh Windows: `.venv\Scripts\day09.exe run`, sau đó
  `.venv\Scripts\day09.exe validate`.
- Secrets chỉ đọc từ `.env`, không được đưa vào trace, output hoặc tài liệu này.
