# L3A Architecture Record

## 1. System overview

`solve_case` tạo một `CaseWorkflow` riêng cho mỗi case, sao chép input và chạy
async state-machine bằng Python 3.11+:

```text
RECEIVED → DISCOVER → COLLECT → POLICY → VERIFY → FINALIZED
                        │         │        │
                  asyncio     MCP policy   schema + consistency
                  TaskGroup   + findings   + provenance linkage
                        └─────────┴────────┴── TraceWriter
```

Ba specialist chạy đồng thời trong `TaskGroup`; policy chờ các handoff hoàn tất.
Verifier phải thành công trước khi trả output. CLI ghi output qua file tạm rồi
replace. Solver sở hữu toàn bộ lifecycle event, CLI không ghi trùng receive/finalize.
`case_finalized` nghĩa là quyết định đã qua verifier; lỗi lưu file của CLI vẫn là lỗi run.

## 2. Agent ownership

| Actor | MCP tool được phép | Trách nhiệm |
| --- | --- | --- |
| coordinator | discovery | Giữ case context, điều phối state, chờ/cancel specialist |
| order-agent | `get_order`, `get_order_items`, `get_sellers` | Xác nhận order, item, seller trong scope |
| payment-agent | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Đối chiếu capture, thanh toán và trạng thái hoàn tiền |
| shipment-agent | `get_shipment_summary` | Đối chiếu thời gian giao, hạn bàn giao và actor |
| policy-agent | `get_policy` | Chọn quy tắc từ findings, lấy tiền/hành động/vai trò chịu trách nhiệm |
| verifier | Không gọi tool | Kiểm schema, ref đã tiêu thụ, tiền, vai trò và lifecycle |

Catalog chỉ gồm adapter cho các tool đã xác nhận qua discovery. Runtime kiểm tra
tool tồn tại và arguments đúng input schema trước mọi call. Catalog schema được
cache theo gateway session; evidence và policy response không cache chéo case.
Refund tool chỉ được route cho claim refund; claim không quyết định kết luận.
Seller records được lấy thêm khi findings yêu cầu trách nhiệm seller.

## 3. A2A protocol

Handoff nội bộ dùng context của `CaseWorkflow`, kèm trace envelope có `case_id`,
`actor`, `target`, `evidence_refs` và decision code. Context tách biệt ngay cả khi
hai case trùng order. Không có remote A2A server hoặc framework/LLM phụ thuộc ngoài.

Mỗi MCP/discovery có timeout 45 giây. Tối đa ba call cùng lúc trong một case.
`TaskGroup` cancel và join các sibling khi có lỗi; state không quay lại nên không
có vòng lặp retry vô hạn. Trace chỉ chứa sự kiện quan sát được, mã quyết định,
domain, hash do gateway trả; không chứa prompt hoặc chain-of-thought.

## 4. Evidence và quyết định

Mọi call truyền `case_id` hiện tại và order từ input hoặc policy version từ input.
Gateway validate evidence envelope. Workflow kiểm domain và các `case_id` /
`order_id` xuất hiện trong data trước khi lưu bản sao và emit
`tool_result_consumed`. Không tạo/sửa evidence ref; các ref trong test là fixture
tổng hợp, chỉ ghi vào thư mục tạm của pytest, tuyệt đối không dùng để nộp bài.
Hash được giữ nguyên từ server; client không giả lập server audit hoặc tự nhận đã
xác minh team/run ownership vì envelope công khai không cung cấp các trường đó.

Phân loại dùng trạng thái order, capture đã confirmed, tổng item + freight,
timeline refund và thời gian bàn giao/giao hàng. Hai khoản bằng nhau cộng đúng tổng
đơn không phải duplicate. Hai capture bằng nhau vượt tổng đơn là dấu hiệu duplicate,
được gán confidence thấp hơn kết luận trực tiếp. Event refund mới nhất thay thế
trạng thái cũ. Nhiều lifecycle độc lập hoặc dữ liệu mâu thuẫn chưa giải quyết phải
được điều tra thêm; không suy ra sự thật từ topic của khách.

Với dữ liệu lặp lại cùng item/order ID, các phiên bản chỉ được chọn khi thời gian
order phân biệt được chúng; nếu không, trả `insufficient_evidence`. Khoảng đối chiếu
là từ purchase tới mốc muộn hơn giữa mở case và delivery được order xác nhận.
`opened_at` không phải snapshot cutoff: giao hàng xác nhận sau khi mở khiếu nại vẫn
có thể giải quyết khiếu nại đó. Việc lọc lifecycle được ghi vào `data_conflicts`.
Đây là quy tắc đối chiếu của implementation, không phải oracle của bộ chấm.

`contracts/scoring/scoring-policy-v2.json` quy định kiểm lifecycle và tiêu chí chấm,
không chứa nghiệp vụ hoàn tiền. Nghiệp vụ lấy từ `get_policy`, kiểm đúng version và
BRL, dùng trực tiếp `refund_brl`, `case_status`, `recommended_action` và party type.
Vai trò seller của policy chung được bind với seller thực tế của case đã đối chiếu
item và seller records. Không dùng seller ID của một case khác trong policy chung.
Tính tiền bằng `Decimal`, kiểm chính xác tới cent; refund không vượt capture đã xác nhận.

Output chỉ giữ evidence đã dùng cho kết luận, entity, đối chiếu và policy.
Payment/shipment ID thiếu thì để mảng rỗng, không tự ghép ID. Claim assessments liên
kết với cùng tập bằng chứng của quyết định. Confidence là heuristic, không phải xác
suất đã fit bằng nhãn: cơ sở 0.95 (duplicate 0.88), trừ 0.05 cho mỗi conflict/warning
domain, và 0.35 khi không đủ bằng chứng. Không xuất confidence 1.0.

## 5. Failure policy

| Failure | Retry | Kết quả |
| --- | --- | --- |
| Timeout / transport / 403 / tool error | Không | Propagate lỗi, không finalize hoặc tự tạo output |
| Tool không discovery được / sai input schema | Không | Dừng trước khi dùng tool không hợp lệ |
| Evidence sai schema/domain/scope | Không | Từ chối evidence, không finalize |
| Thiếu dữ kiện trong response hợp lệ | Không | `insufficient_evidence`, `needs_investigation`, refund 0 |
| Mâu thuẫn không phân xử được / policy vượt capture | Không | Ghi conflict, không quy trách nhiệm hoặc duyệt tiền |
| Verifier không pass | Không | Không finalize |

Specialist thất bại ghi handoff về coordinator với `EVIDENCE_COLLECTION_FAILED`
và error type; không ghi lỗi đó thành `tool_result_consumed`. Không retry tự động vì
request timeout có thể đã được audit ở server. CLI dừng khi case lỗi; chạy lại sẽ
tạo mới output/trace theo hành vi có sẵn của starter.

## 6. Verification invariants

- Output đúng public schema, case ID, confidence bounds, unique actions.
- Tất cả ref được lấy nguyên vẹn từ response đã tiêu thụ trong case; claim refs là
  tập con của output refs.
- Tổng refund lines bằng recommendation, entity mỗi line đúng order.
- Tiền/action/status/party type khớp policy; không hoàn tiền khi `no_action`.
- Seller responsibility thuộc affected sellers; seller delay và logistics delay
  không được gán chéo bên chịu trách nhiệm.
- Thiếu bằng chứng không được phê duyệt tiền hoặc quy trách nhiệm.
- Conflict chọn source phải nằm trong sources; có conflict thì confidence ≤ 0.9.
- Lifecycle trước finalize có đủ receive, assign, consume, handoff, policy_decided;
  verifier thành công mới ghi verification_completed và case_finalized.

## 7. Reproducibility

Không dùng LLM hoặc random seed trong policy engine. Event ID và evidence ref là
định danh runtime; không ảnh hưởng quyết định. Dependencies vẫn theo khoảng version
của `pyproject.toml`; lần triển khai này kiểm thử với Python 3.12, MCP 2.2.0,
httpx2 2.13.1, jsonschema 4.26.0, pytest 8.4.2.

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q tests/test_starter.py tests/test_workflow.py tests/test_gateway.py
python -m ruff check src tests
python -m student_agent.cli validate-inputs
python -m student_agent.cli run
python -m student_agent.cli validate
```

Máy hiện tại dùng `.workflow-venv/Scripts/python.exe` vì `.venv` cũ tham chiếu Python
không còn tồn tại. Không đưa environment, input, inspection hoặc credential vào Git/ZIP.
Test `test_repository_contains_no_competition_payload` dành cho starter release sạch:
sẽ fail khi workspace đã giải nén `case-set.json` và inputs theo README; giữ nguyên
test và dữ liệu, không xóa input để làm test xanh.
