# L3A Architecture Record

## 1. System overview

Triển khai tại `src/student_agent/workflow.py`, entry point `solve_case(case, gateway, trace)`.
Đây là workflow gồm các specialist theo quy tắc xác định, chạy trong cùng process;
không gọi LLM, không yêu cầu API key Gemini/OpenAI và không dùng framework agent ngoài.

```text
CLI: case_received
  → Coordinator: xác định case/order, discover tools
  → 4 actor thu thập evidence song song, mỗi actor gọi tool tuần tự
      order-agent     → order + items
      payment-agent   → payment timeline + refund timeline
      shipment-agent  → shipment summary
      policy-agent    → policy theo version của case
  → Handoff evidence về coordinator
  → order_specialist → payment_specialist → shipment_specialist
  → policy_specialist: đề xuất action/refund theo policy
  → Verifier: schema + scope + evidence + tiền + seller
  → Return output → CLI ghi outputs/<case_id>.json và case_finalized
```

Customer message và claim topic chỉ là yêu cầu cần kiểm tra, không quyết định
`primary_issue`. Workflow không đọc output tham chiếu hoặc tài liệu chấm riêng.

## 2. Agent ownership

| Actor | Input | Trách nhiệm / tool được phép | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case, tool discovery | Chia việc, tổng hợp, chuyển verifier; không tự tạo evidence | `Investigation` riêng cho case và output dự thảo |
| order-agent | `case_id`, claimed order ID | `get_order`, `get_order_items`; đối chiếu ID, thời gian, item trùng | Order, items hợp lệ, cửa sổ thời gian |
| payment-agent | Case/order ID, items | `get_payment_timeline`, `get_refund_timeline`; capture, mismatch, split payment, trạng thái refund | Số tiền đã capture, tổng giá hàng + freight, vấn đề payment/refund |
| shipment-agent | Case/order ID, order/items | `get_shipment_summary`; đối chiếu thời gian, deadline và handoff | Vấn đề giao hàng và seller có handoff trễ |
| policy-agent | `policy_version`, vấn đề đã xác minh | `get_policy`; áp dụng đúng rule, số tiền, status/action | Đề xuất giải quyết có policy evidence |
| verifier | Output dự thảo và evidence của case | Không gọi tool; kiểm tra invariants | `verification_completed` hoặc lỗi nếu output không hợp lệ |

`OWNERSHIP` kiểm soát tool theo actor; chỉ gọi tool có trong `list_tools()`.
Các tool customer history/product context/sellers chưa dùng vì logic hiện tại
không cần các domain này. Nếu mở rộng nghiệp vụ phải mở rộng quyền và test tương ứng.

## 3. A2A protocol

A2A ở đây là handoff nội bộ process, chưa phải dịch vụ A2A HTTP độc lập.
Envelope quan sát được là trace event: `case_id`, `actor`, `target`, `event_type`,
`decision_code`, `evidence_refs`; `TraceWriter` thêm event ID và timestamp.
Payload bằng chứng nằm trong `Investigation.evidence[tool_name]`, chỉ tồn tại cho
một lần `solve_case`. Các hàm specialist xử lý payload và trả kết quả có cấu trúc
cho coordinator. Không truyền nội dung suy luận riêng qua trace.

- Coordinator emit `task_assigned` trước khi actor thu thập evidence.
- Actor emit `tool_result_consumed` cho response đã validate, sau đó `handoff`
  với `EVIDENCE_READY` hoặc `EVIDENCE_INCOMPLETE`.
- Policy emit `policy_decided` khi rule đã được áp dụng.
- Coordinator handoff output cho verifier; CLI quản lý hai sự kiện đầu/cuối case.
- Đồ thị cố định, không có handoff ngược hoặc vòng lặp tự giao việc.
- Tối đa 4 actor đang thu thập đồng thời; từng actor gọi tool tuần tự.
- Timeout mỗi call 30 giây; timeout/ConnectionError thử lại tối đa 1 lần sau 0,25 giây.
  Một case gọi tối đa 6 tool evidence, tối đa 12 lần nếu tất cả đều cần retry,
  cộng tool discovery. Discovery dùng timeout của transport trong gateway.

## 4. Evidence lifecycle

1. Dùng đúng `case_id` và order ID từ case cho các truy vấn order-scoped.
2. Gateway validate `mcp-evidence-response-v1.schema.json`. Gateway hỗ trợ field
   `is_error`/`structured_content` của SDK MCP 2.x và dạng camelCase cũ.
3. Workflow kiểm tra domain của từng tool và kiểm tra đệ quy `order_id` trong payload.
   Bằng chứng có order khác bị loại, không được gắn vào output hoặc trace consumed.
4. Giữ nguyên `evidence_ref` do server cấp. Không sửa, sinh hoặc cache ref qua case.
   Chỉ server audit xác nhận được team/run ownership; schema response không có
   các field đó, nên client không tự tuyên bố xác minh chữ ký/hash hoặc audit ownership.
5. Xét các timeline event trong khoảng thời gian purchase → `opened_at`, có timezone.
   Event ngoài khoảng bị loại và ghi conflict. Đây là quyết định xử lý as-of của
   implementation, không phải khẳng định schema input tự yêu cầu chính sách này.
6. Item trùng hoàn toàn được gộp. Nếu cùng ID nhưng khác dữ liệu, chỉ chọn khi có
   duy nhất một phiên bản có shipping limit trong cửa sổ case; nếu mơ hồ thì điều tra thêm.
7. Evidence được liên kết từ output và claim assessments, đồng thời có trace consumed.
   Bản hiện tại giữ cả các domain đã dùng để kiểm tra/loại trừ; chưa tối ưu tập ref tối thiểu.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / ConnectionError | Tối đa 1 lần | Giữ các evidence đã xác minh; thiếu evidence bắt buộc thì điều tra thêm | `handoff / EVIDENCE_INCOMPLETE` |
| Tool không discover được hoặc tool trả lỗi | Không | Không coi lỗi là response rỗng; `insufficient_evidence` nếu cần domain đó | `handoff / EVIDENCE_INCOMPLETE` |
| Not found / payload thiếu field | Không | Không đoán order, số tiền, ref | Handoff, rồi verifier với output điều tra thêm |
| Source conflict | Không | Chọn theo cửa sổ case nếu duy nhất; còn mâu thuẫn thì điều tra thêm | `data_conflicts` trong output |
| Invalid specialist result / policy | Không | Không đề xuất refund; action `request_manual_review` | Handoff đến verifier |
| Output không qua verifier/schema | Không | Dừng trước ghi output; sửa logic, không nộp dữ liệu sai | Không emit verification thành công |

Các tool hiện dùng chỉ đọc evidence, không tạo giao dịch tài chính. Retry có thể
sinh audit/ref mới nhưng không thực hiện lệnh hoàn tiền. Workflow không retry lỗi
nghiệp vụ hoặc lỗi payload bằng cách đoán lại tên tool/tham số.

`get_refund_timeline` trả lỗi ở một số lần thử thực tế. Không có bằng chứng hoàn tiền
không có nghĩa là chưa hoàn tiền; hiện workflow chủ động abstain khi tool này lỗi.
Trạng thái refund completed/confirmed cũng chặn payout mới cho đến khi có quy tắc
đối soát số tiền còn lại. Đây là giới hạn bảo thủ, có thể làm giảm độ phủ case.

## 6. Verification invariants

- Output đúng JSON Schema V2 và đúng case ID.
- Mỗi ref trong output tồn tại trong evidence của chính lần solve; claim refs là tập con output refs.
- Không lấy seller ID từ policy template của order khác; seller chịu trách nhiệm phải có trong items.
- Không tự ghép `payment_sequential` thành payment reference hay tự tạo shipment ID;
  hai tập ID này để rỗng khi các evidence đã dùng không cung cấp ID phù hợp.
- Dùng `Decimal`, làm tròn 2 chữ số; số tiền hữu hạn, không âm; tổng refund lines bằng tổng đề xuất.
- Refund theo rule công khai, không vượt tổng capture đã chứng minh; không trả thêm khi có refund hoàn tất.
- `no_action` không đi kèm số tiền refund dương. Status/action lấy từ policy, không từ lời khách hàng.
- Confidence thuộc [0,1]: 0,9 khi đủ evidence không conflict; 0,75 khi đã giải quyết conflict;
  0,2 khi thiếu bằng chứng. Đây là heuristic chưa được hiệu chỉnh bằng điểm chấm.

Thứ tự ưu tiên phân loại: refund lifecycle → canceled/unavailable có capture → giao hàng trễ
→ payment → unsupported khi đơn đã giao và thanh toán đủ. Duplicate charge cần event
xác nhận `duplicate_charge`; chỉ nhiều dòng payment hoặc trả dư chưa đủ chứng minh bị tính tiền trùng.
Valid split cần nhiều capture cộng đúng tổng item + freight. Trách nhiệm giao chậm dựa vào
carrier handoff so với shipping limit và đối chiếu actor trong shipment event.

Claim `requested_full_refund` so số tiền đề xuất với tổng capture; claim khác chỉ được
supported khi trùng vấn đề đã chứng minh. Không suy ra mọi claim khác đều sai từ một primary issue.

## 7. Reproducibility

- Python >=3.11; cài `python -m pip install -e ".[dev]"`.
- Không dùng LLM, không có temperature/seed. Quy tắc và dữ liệu quyết định kết quả;
  timestamp/event ID/ref của server vẫn có thể khác giữa các lần chạy.
- Dependency dùng khoảng phiên bản trong `pyproject.toml`; chưa có lockfile đầy đủ,
  vì vậy chưa cam kết byte-for-byte reproducibility giữa hai môi trường cài khác ngày.
- Cấu hình kết nối lấy từ `.env`: `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`,
  `MCP_ENDPOINT`. Không ghi giá trị secret vào tài liệu, trace hoặc test.
- CLI chạy case tuần tự; trong case tối đa 4 actor thu thập song song.

```bash
source .venv/bin/activate
day09 validate-inputs
day09 mcp-tools
pytest tests/test_starter.py tests/test_workflow.py -q
ruff check src/student_agent/workflow.py src/student_agent/mcp_gateway.py tests/test_workflow.py
day09 run
day09 validate
day09 package --output dist/submission.zip
```

`day09 run` xóa outputs/trace cũ trước khi chạy: lưu bản cần giữ trước khi chạy lại.
Test release-safety của starter yêu cầu không có `case-set.json`, nên không đạt trong
workspace đã tải input; không xóa bộ input hoặc sửa test để che tình trạng này.

Tests workflow dùng fake gateway, không gọi mạng: 10 nhánh vấn đề, claim không đáng tin,
scope sai, event tương lai, thiếu refund service, refund hoàn tất, retry có giới hạn,
item conflict, refund vượt capture và tương thích MCP 2.x. Test pass không phải điểm
semantic/provenance của competition. Muốn có kết quả chấm phải chạy 100 case với gateway
thật rồi nộp ZIP đúng quy định; implementation không tự upload bài.

### Kiểm tra ngày 25/09/2026

- `pytest tests/test_starter.py tests/test_workflow.py -q`: **21 passed**.
- Toàn suite: **22 passed, 1 failed**; lỗi duy nhất là test release-safety yêu cầu
  không có `case-set.json` trong workspace (bộ input đã được tải theo README).
- Ruff trên các file Python sửa/thêm: đạt; `git diff --check`: đạt.
- Smoke test qua MCP thật, output qua schema: case 001 → `insufficient_evidence`
  (5 refs; refund tool không cung cấp evidence); case 008 → `refund_pending`
  (6 refs); case 009 → `refund_failed` (6 refs).
- Chưa chạy workflow thực tế trên toàn bộ 100 case và chưa nộp/chấm điểm. Kết quả
  smoke chỉ chứng minh tích hợp và schema cho 3 case, không chứng minh điểm semantic.
