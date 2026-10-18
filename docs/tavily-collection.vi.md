# F001: thu thập nguồn web trước Research

`collection_mode` có hai giá trị. `manual` là mặc định, giữ bốn bước và yêu cầu
`company_description` không rỗng. `tavily` chạy
**COLLECT → RESEARCH → CLASSIFY → SCORE → REPORT**, chỉ yêu cầu tên công ty và ICP;
website và văn bản operator đều tùy chọn. Service gọi Tavily trực tiếp qua async HTTP;
agent không được tự gọi tool hay quyết định search. Ba bước LLM vẫn dùng OpenAI.

## Cấu hình và chạy

Chạy migration trước khi khởi động lại API và worker:

```powershell
.\.venv\Scripts\alembic upgrade head
```

Revision `26a1018_tavily_collection` thêm `ai_jobs.workflow_version` và hai cột audit
`collection_started_at`, `collection_usage` của attempt. Job hiện có được gán
`manual-v1`; không thêm COLLECT hoặc sửa input/checkpoint cũ. Job Tavily mới dùng
`tavily-v1`. Không cần dependency mới hoặc migration dữ liệu output.

Thêm các biến sau vào `.env` cục bộ rồi khởi động lại worker. Với Docker, build và
tạo lại service theo hướng dẫn README; migration service phải hoàn tất trước API/worker.

```dotenv
TAVILY_API_KEY=<your-local-key>
TAVILY_REQUEST_TIMEOUT_SECONDS=30
TAVILY_MAX_RESULTS=5
COLLECT_MAX_ATTEMPTS=2
```

Key chỉ cần cho Tavily mode. Manual không tạo collector/HTTP client và không yêu cầu
key này. `/ready` kiểm tra schema DB và Redis; không gọi Tavily hoặc OpenAI.
`TAVILY_MAX_RESULTS` nằm trong 1–5; timeout dương và nhỏ hơn lease TTL hơn 10 giây.
Ceiling COLLECT là `min(COLLECT_MAX_ATTEMPTS, MAX_STEP_ATTEMPTS)` cho cả retry tự động
và thủ công. Giới hạn LLM/ước tính USD vẫn áp dụng riêng cho ba bước LLM.

Payload tối thiểu tại [examples/research/tavily-company.json](../examples/research/tavily-company.json):

```json
{
  "company_name": "Example Company",
  "website": "https://example.com",
  "icp": "B2B SaaS for SMEs in Vietnam",
  "collection_mode": "tavily"
}
```

Tên công ty và URL trên là placeholder để xem hợp đồng API. Thay bằng công ty cần
nghiên cứu trước khi chạy thực tế. Gửi `POST /api/v1/research/jobs`, header
`Content-Type: application/json` và `Idempotency-Key` mới, rồi poll GET theo job ID
như README. Gửi request thực tế sử dụng dịch vụ Tavily và OpenAI có tính phí.
Tests mặc định dùng HTTP/SDK mock; không chạy smoke trả phí trong quá trình kiểm thử.

## Nguồn, checkpoint và provenance

Mỗi attempt COLLECT gửi tối đa một Search POST với `search_depth=basic`, không dùng
generated answer và không fetch URL từ backend. Query v1 chỉ chứa tên công ty,
hostname website nếu có và các từ tìm giới thiệu/sản phẩm/khách hàng; ICP không đi
vào query hoặc trở thành evidence về công ty.

Adapter chuẩn hóa HTTP(S) URL, bỏ fragment, loại URL không hợp lệ/credentials,
nguồn trùng lặp và text rỗng. Domain chính thức được ưu tiên bằng hostname/subdomain
đúng ranh giới; thứ hạng không chứng minh danh tính. Text mỗi nguồn tối đa 4.000 ký tự,
tổng tối đa 20.000, tối đa 5 nguồn. Checkpoint lưu chính phần text sau chuẩn hóa/cắt
giới hạn, URL, ID nguồn ổn định, timestamp UTC, warning và usage nếu provider có trả.
Warnings nêu giới hạn snippet, domain không tìm thấy hoặc tên công ty không chắc khớp.

GET job trả bước COLLECT và `result.collect` ngay khi checkpoint có sẵn, kể cả khi
bước sau chưa hoàn tất. `research.evidence` vẫn là `list[str]`; `evidence_sources`
thêm mapping `{quote, source_id}`. Từng quote phải xuất hiện nguyên văn trong content
của nguồn được tham chiếu. ID giả hoặc quote ngoài nguồn bị từ chối. CLASSIFY chỉ
dùng evidence đã kiểm tra; SCORE giữ quy tắc chấm điểm hiện có.

Operator text có ID `operator:company_description` hoặc `operator:website_text` và
URL `null`; website được nhập không chứng minh nguồn gốc của text đó. Văn bản operator
được giữ riêng với nguồn web, không ghi đè `job.input_json`. Báo cáo có `sources`
(`source_id`, URL chính xác) và `limitations`. Validator ngăn URL nguồn giả và yêu cầu
trích nguồn của evidence đã dùng. Prompt yêu cầu trình bày hạn chế; kiểm tra provenance
không xác minh sự thật hoặc bảo đảm tuyệt đối chống prompt injection.

Hash COLLECT gồm identity chuẩn hóa, giới hạn và version policy/schema, không có key,
ICP, thời gian chạy hay model LLM. RESEARCH hash gồm nguồn hiệu lực và metadata
collection liên quan. Resume tái sử dụng checkpoint, không search lại do bước sau bị
gián đoạn. Hash/schema/prompt manual cũ được giữ, và idempotency so sánh nhất quán các
default mới với input legacy mà không sửa bản lưu.

## Lỗi, retry và chi phí

| Tình huống | Hành vi |
|---|---|
| Thiếu key, 401/403, request/configuration sai | COLLECT NEEDS_REVIEW, lỗi sanitize, không gọi LLM |
| HTTP 429 được xác nhận | Backoff hữu hạn qua DB, tối đa ceiling attempt |
| Timeout, network error, 5xx, crash không rõ kết quả | UNKNOWN / NEEDS_REVIEW, không tự gọi lại |
| Response sai schema | NEEDS_REVIEW, không checkpoint |
| Không có web source nhưng có operator text | Checkpoint rỗng có warning, tiếp tục bằng operator source |
| Không có nguồn nào | `COLLECT_NO_USABLE_SOURCES`, không gọi LLM |
| Lease hết hạn | Kết quả muộn bị fencing chặn; không ghi đè checkpoint |

Không fallback manual khi search lỗi. Admin chỉ retry bước chưa xong tiếp theo, còn
dưới ceiling, qua `POST /api/v1/research/jobs/{job_id}/retry` với bearer admin token và:

```json
{"step_name":"COLLECT","acknowledge_possible_duplicate_charge":true}
```

Kiểm tra events trước khi retry: request cũ có thể đã được xử lý/tính phí. Không bảo
đảm exactly-once billing. Trước dispatch, một transaction ghi intent và tăng bộ đếm
riêng; không giữ DB transaction trong lúc chờ HTTP. Lease/heartbeat/fencing vẫn áp dụng.

Admin metrics trả `collection_calls` (số intent, không chứng minh số request provider
đã xử lý), `collection_usage` và `collection_credits` riêng với `llm_calls`, token và
`estimated_cost_usd`. Key/configuration thiếu cũng có thể có intent đã ghi. Credits
không được quy đổi sang USD; `null` nghĩa là chưa biết. Tổng credits chỉ bao gồm usage
đã checkpoint, không đại diện mọi request thất bại/không rõ kết quả. Attempt COLLECT
không có giá USD khi chưa biết giá; không dùng token pricing để ghi chi phí Tavily.

Log chỉ có ID, attempt, latency, số nguồn, usage số và category lỗi. Query, URL, content
và key không vào log/metadata mặc định. Langfuse có span COLLECT kiểu
`external_collection`, không phải OpenAI generation; input/output chỉ được xuất khi
`LANGFUSE_CAPTURE_CONTENT=true`. Cân nhắc nội dung công ty trước khi bật capture.

REST contract được đối chiếu ngày 2026-10-18 với
[Tavily Search reference](https://docs.tavily.com/documentation/api-reference/endpoint/search).
Kết quả tìm kiếm là dữ liệu nguồn có thể thiếu/cũ/sai; không phải chứng nhận sự thật.
