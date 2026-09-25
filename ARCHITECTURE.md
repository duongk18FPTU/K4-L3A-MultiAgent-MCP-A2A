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
CLI gọi `runner.solve_with_reconnect`: mỗi case/attempt dùng MCP session mới.
Không giữ một session HTTP xuyên suốt 100 case. Khi stream chết, retry tạo lại
cả HTTP client và MCP session, chạy lại case chưa hoàn tất với context/evidence mới.

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

Mỗi MCP/discovery và bước initialize có timeout 45 giây. Tối đa ba call cùng lúc
trong một case; CLI xử lý các case tuần tự.
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
| ReadError / ConnectError / network timeout / stream đóng | Tối đa 3 attempts/case | Session mới, backoff 1s rồi 2s, chạy lại case chưa hoàn tất |
| HTTP 429 / 500 / 502 / 503 / 504 từ transport | Cùng giới hạn trên | Reconnect; không diễn giải thành evidence |
| 401 / 403 / tool error nghiệp vụ | Không | Propagate lỗi, không finalize hoặc tự tạo output |
| Tool không discovery được / sai input schema | Không | Dừng trước khi dùng tool không hợp lệ |
| Evidence sai schema/domain/scope | Không | Từ chối evidence, không finalize |
| Thiếu dữ kiện trong response hợp lệ | Không | `insufficient_evidence`, `needs_investigation`, refund 0 |
| Mâu thuẫn không phân xử được / policy vượt capture | Không | Ghi conflict, không quy trách nhiệm hoặc duyệt tiền |
| Verifier không pass | Không | Không finalize |

Specialist thất bại ghi handoff về coordinator với `EVIDENCE_COLLECTION_FAILED`
khi lỗi được workflow bắt; lỗi background transport còn có thể xuất hiện dưới dạng
`ExceptionGroup` khi session đóng. Runner phân loại tất cả leaf exception, kể cả
`EvidenceError` bọc lỗi TaskGroup: chỉ retry khi mọi lỗi là transport có thể phục hồi.
Nhóm lỗi trộn schema/scope với transport không được retry. Cancellation được propagate.
HTTP response hook giữ mã lỗi POST trước khi MCP SDK đổi HTTP non-2xx thành
`MCPError: Server returned an error response`. Không ghi response body hoặc header
Authorization. GET/DELETE tùy chọn vẫn do SDK xử lý, tránh coi 405 khi đóng session
là lỗi điều tra. Không retry MCP internal error chung chỉ dựa vào thông báo mơ hồ.

Retry sau khi bắt đầu điều tra ghi event hợp lệ `handoff`, decision code
`MCP_RECONNECT`, metadata trong `attributes` (attempt, next_attempt, delay_seconds,
error_type). Không thêm field/schema hoặc event type mới. Nếu initialize chưa thành
công thì log retry ra stderr, giữ `case_received` là event đầu tiên của lifecycle.
Không xóa trace lần thất bại; output chỉ dùng ref mới lấy trong attempt thành công.
Calls lặp lại có thể đã được server audit: đây là retry các tool đọc, không phải
cam kết exactly-once và không tái sử dụng ref để che số lần gọi.

Nếu chỉ mất kết nối trong teardown sau khi solver đã hoàn tất và output đã validate,
giữ kết quả đã xác minh; không chạy lại hoặc ghi thêm finalize. Hết 3 attempts thì
dừng với thông báo gọn, giữ output các case trước. `day09 run --resume` kiểm schema,
case/order scope, refs đã consumed và đúng policy/verification/finalize trong cùng
attempt trước khi bỏ qua output đã hoàn tất. Giữ nguyên trace cũ và chạy mới case
chưa có output; không tái dùng evidence một phần. Artifact hỏng/thiếu trace bị từ
chối, không tự xóa hoặc sửa trace. `run` không có `--resume` vẫn chạy mới từ đầu.
Chỉ resume trong cùng competition run/team/case-set phía server; local schema và
trace không thể xác nhận lại server-side ownership khi server đã reset run.

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

Public contracts trong `contracts/schemas/` giữ nguyên. Test `test_contract_lock.py`
khóa SHA-256 trên nội dung JSON canonical của cả 5 schema (không phụ thuộc CRLF/LF),
kiểm schema hợp lệ và việc từ chối field ngoài schema. Không sửa schema để hợp thức
hóa output. Envelope, trace, output và manifest đều qua validator công khai của repo.

Không dùng LLM hoặc random seed trong policy engine. Event ID và evidence ref là
định danh runtime; không ảnh hưởng quyết định. Dependencies vẫn theo khoảng version
của `pyproject.toml`; lần triển khai này kiểm thử với Python 3.12, MCP 2.2.0,
httpx2 2.13.1, jsonschema 4.26.0, pytest 8.4.2.

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q tests/test_starter.py tests/test_workflow.py tests/test_gateway.py tests/test_runner.py tests/test_contract_lock.py
python -m ruff check src tests
python -m student_agent.cli validate-inputs
python -m student_agent.cli run
python -m student_agent.cli run --resume
python -m student_agent.cli validate
```

Máy hiện tại dùng `.workflow-venv/Scripts/python.exe` vì `.venv` cũ tham chiếu Python
không còn tồn tại. Không đưa environment, input, inspection hoặc credential vào Git/ZIP.
Test `test_repository_contains_no_competition_payload` dành cho starter release sạch:
sẽ fail khi workspace đã giải nén `case-set.json` và inputs theo README; giữ nguyên
test và dữ liệu, không xóa input để làm test xanh.
