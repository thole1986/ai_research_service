# F001 — Thu thập nguồn web bằng Tavily trước Research

- Trạng thái: IMPLEMENTED — 2026-10-18.
- Ngày tạo: 2026-10-17.
- Lựa chọn đã thống nhất: service gọi Tavily trước Research (cách 1).
- Đặc tả đã được triển khai; bằng chứng kiểm tra và giới hạn nằm tại mục 11.

## 1. Mục tiêu và phạm vi

Cho phép người dùng gửi tên công ty, website nếu biết và ICP; service tự thu thập
nguồn web bằng Tavily rồi chạy phân tích dựa trên nguồn đã lưu.
Giữ chế độ nhập văn bản thủ công và bảo đảm job cũ tiếp tục hoạt động.

Luồng mới khi bật thu thập:

```text
Input → COLLECT (Tavily, không LLM) → RESEARCH → CLASSIFY → SCORE → REPORT
```

Luồng thủ công tiếp tục có bốn bước như trước.
Không để agent tự quyết định gọi tool trong feature này.
Không thay provider LLM, không sửa quy tắc chấm điểm để tối ưu tiếng Việt,
không thêm UI, crawler trực tiếp, deep research hay vector database.

Kết quả tìm kiếm là dữ liệu nguồn, không phải chứng nhận đúng sự thật.
Tavily bổ sung khả năng tìm dữ liệu; OpenAI vẫn xử lý ba bước LLM.

## 2. Các điểm trong code cần đọc trước khi triển khai

| Khu vực | File hoặc thư mục hiện tại | Điểm cần chú ý |
|---|---|---|
| API | `src/company_ai/api/research/schemas.py` | Input, retry literal, response |
| Domain | `src/company_ai/domain/research/contracts.py` | STEPS cố định, AIResult, lỗi |
| Domain | `src/company_ai/domain/research/entities.py` | Thứ tự bước và retry candidate |
| Domain | `src/company_ai/domain/research/outputs.py` | Hợp đồng đầu ra |
| Domain | `src/company_ai/domain/research/policies.py` | Provenance và stale policy |
| Scoring | `src/company_ai/domain/research/scoring.py` | Đang đọc evidence dạng list[str] |
| Workflow | `src/company_ai/application/research/execute_workflow.py` | Payload, hash, dispatch, reserve cost, telemetry, lỗi |
| Ports | `src/company_ai/application/research/ports.py` | Repository và collector port mới |
| Prompts | `src/company_ai/application/research/prompts.py` | Nguồn hợp lệ và ghi chú báo cáo |
| Persistence | `src/company_ai/infrastructure/research/persistence/` | Tạo/sắp xếp step, claim, recovery, retry, metrics |
| Wiring | `src/company_ai/bootstrap/`, `src/company_ai/runtime.py`, `src/company_ai/tasks/` | Lifecycle, inject collector vào worker |
| Settings | `src/company_ai/settings.py`, `.env.example` | Config mới, không lưu secret vào job |
| Tests/docs | `tests/`, `docs/`, `examples/research/` | Hồi quy, demo, tài liệu vận hành |

Tìm toàn repo các chỗ dùng STEPS, SCORE, model/prompt version, số bước và
`execution_type`; không giả định thêm một phần tử vào STEPS là đủ.

## 3. Hợp đồng API và tương thích

Thêm trường tùy chọn `collection_mode`: `manual` hoặc `tavily`, mặc định `manual`.
Tiếp tục dùng endpoint tạo job hiện tại và Idempotency-Key.

### Chế độ manual

- Giữ yêu cầu `company_name`, `company_description` không rỗng và `icp`.
- `website`, `website_text` vẫn tùy chọn.
- Tạo đúng bốn bước cũ, không gọi Tavily và không yêu cầu Tavily API key.
- Payload cũ thiếu collection_mode được hiểu là manual.

### Chế độ tavily

- Bắt buộc `company_name` và `icp`; `website` tùy chọn.
- Cho phép company_description thiếu/rỗng; website_text mặc định rỗng.
- Văn bản do người dùng gửi vẫn là nguồn riêng, không bị ghi đè bởi web.
- Tạo năm bước với COLLECT đầu tiên.
- ICP chỉ dùng để đánh giá sau thu thập; không đưa ICP vào truy vấn tìm kiếm
  mặc định và không coi nó là bằng chứng về công ty.

Ví dụ request mới:

```json
{
  "company_name": "Example Company",
  "website": "https://example.com",
  "icp": "Doanh nghiệp B2B SaaS phục vụ SME tại Việt Nam",
  "collection_mode": "tavily"
}
```

GET job trả bước COLLECT và `result.collect` khi có; output cũ vẫn đọc được.
RetryInput chấp nhận COLLECT với cơ chế admin và xác nhận khả năng tính phí trùng.

Idempotency: kiểm tra cách canonical payload được lưu hiện tại. Thêm default mới
không được làm request manual cũ cùng key bất ngờ trả 409. Chuẩn hóa default/field
thiếu nhất quán cho cả bản lưu cũ và request mới; payload khác thực sự vẫn trả 409.

## 4. Thiết kế thu thập

### 4.1 Port và adapter

- Định nghĩa `CompanySourceCollector` async trong application; nhận input thu thập
  đã chuẩn hóa và trả kết quả có kiểu rõ ràng.
- Adapter Tavily nằm trong infrastructure, tách khỏi CrewAIAdapter.
- Ưu tiên AsyncClient của httpx sẵn có để gọi REST API; kiểm tra tài liệu Tavily
  chính thức tại thời điểm triển khai về endpoint, schema, auth, usage và lỗi.
- Quản lý client theo lifecycle worker; đóng client trước khi event loop kết thúc.
- Không giữ DB transaction trong lúc gọi mạng; giữ heartbeat và fencing.
- Không dùng AIResult như một hợp đồng giả cho search nếu gây lẫn token/cost LLM;
  tách execution result chung hoặc tạo result dành cho collection phù hợp.

### 4.2 Chính sách query v1

- Tối đa một request Search mỗi attempt COLLECT trong phiên bản đầu.
- Query xác định từ tên công ty và hostname website nếu có; tìm giới thiệu,
  sản phẩm/dịch vụ và khách hàng mục tiêu.
- Tối đa 5 kết quả, search depth basic; không dùng Tavily-generated answer làm evidence.
- Ưu tiên domain chính thức trong kết quả, nhưng không coi thứ hạng search là
  xác nhận danh tính công ty. Gắn cảnh báo khi domain/tên có dấu hiệu không khớp.
- Không fetch URL trực tiếp ở backend trong feature này. Chỉ lưu text được Tavily trả về.
- Loại kết quả URL không hợp lệ/không phải HTTP(S), trùng lặp hoặc không có nội dung.
- Dùng parser hostname; không so khớp domain bằng phép contains dễ nhận nhầm.
- Giới hạn nội dung mỗi nguồn 4.000 ký tự và tổng nguồn web 20.000 ký tự.
  Lưu chính xác phần text sau chuẩn hóa/cắt giới hạn mà model được phép trích.
- Không khẳng định snippet là toàn bộ trang hoặc thông tin đầy đủ/cập nhật.

### 4.3 Output COLLECT và bằng chứng

Định nghĩa schema tối thiểu:

```text
collection_version
provider = tavily
query
collected_at (UTC)
sources[]:
  source_id (ID ổn định trong checkpoint)
  url
  title
  content (text thực tế đưa vào Research)
  retrieved_at (UTC)
  source_type = web
warnings[]
provider_request_id (nếu API có)
usage/credits (nếu API có trả; không tự suy đoán là chính xác)
```

Lưu checkpoint bền vững, không chỉ lưu URL. Không sửa job.input_json sau khi tạo.
Tạo danh sách nguồn hiệu lực từ văn bản operator và checkpoint COLLECT.
Nguồn operator có source_id và source_type riêng; URL người dùng gửi chưa chứng minh
đó là nguồn của văn bản họ nhập.

Giữ `research.evidence` dạng list[str] để không phá scoring hiện tại. Với job mới có
collection, thêm mapping provenance có cấu trúc giữa từng quote và source_id;
schema phải định nghĩa rõ, kiểm tra tham chiếu và không yêu cầu migration sửa output cũ.
Mỗi quote phải là substring nguyên văn trong content của nguồn được tham chiếu.
Classification tiếp tục dùng evidence đã kiểm tra, không lấy nội dung ICP làm nguồn.
Report hiển thị được URL nguồn và hạn chế; không tự tạo URL hoặc khẳng định xác minh độc lập.

## 5. Workflow, hash và persistence

- Tách danh sách bước theo workflow version/mode. Dùng danh sách step thực tế của job
  trong kiểm tra predecessor, completion và retry; job cũ không bị bắt có COLLECT.
- Bổ sung workflow_version cho job mới theo cách phù hợp schema hiện tại; job cũ được
  diễn giải là legacy manual. Nếu cần schema DB mới, thêm Alembic migration, không sửa
  migration đã áp dụng và không gọi create_all khi startup.
- COLLECT có execution_type external_collection, không có prompt/model LLM.
- COLLECT input hash gồm tên công ty, website chuẩn hóa, query policy version,
  cấu hình ảnh hưởng kết quả và schema version; không có secret hay thời điểm runtime.
- RESEARCH hash bao gồm nguồn hiệu lực và collection output liên quan.
- Checkpoint COLLECT hoàn thành được tái sử dụng khi resume; không search lại do
  Research/Classify/Report bị gián đoạn.
- Không tự thêm COLLECT vào job cũ đang chạy. Không làm đổi hash legacy khiến tất cả
  checkpoint cũ mismatch chỉ vì triển khai feature. Giữ đường hash/schema/prompt legacy
  khi cần; thay đổi prompt/schema mới phải có version riêng cho job mới.
- Tiếp tục claim/attempt atomically, lease, heartbeat và fencing; worker hết lease
  không thể ghi đè checkpoint.
- Scan pending, stale recovery, admin retry và metrics phải hiểu kiểu bước mới.

## 6. Lỗi, retry và kiểm soát chi phí

COLLECT là lời gọi dịch vụ trả phí, không phải phép tính deterministic như SCORE.
Không tự động gọi lại khi timeout/crash có khả năng request đã được xử lý.

| Tình huống | Hành vi v1 |
|---|---|
| Thiếu key/cấu hình không hợp lệ, 401/403 | NEEDS_REVIEW, lỗi được sanitize, không gọi LLM |
| 429 được xác nhận từ provider | Backoff hữu hạn qua DB; tôn trọng trần attempt |
| Timeout, network error, 5xx hoặc crash sau dispatch chưa rõ kết quả | UNKNOWN / NEEDS_REVIEW; admin quyết định retry |
| Response sai schema | NEEDS_REVIEW, không tạo checkpoint hợp lệ |
| Không có web result dùng được, có operator text | Checkpoint rỗng có warning, tiếp tục với operator text |
| Không có web result và không có operator text | NEEDS_REVIEW với COLLECT_NO_USABLE_SOURCES, không gọi LLM |
| Mất lease | Bỏ kết quả muộn, không ghi checkpoint |

Không fallback manual âm thầm khi search lỗi. Không bật retry ngầm trong HTTP client.
Trước HTTP dispatch, ghi intent và tăng bộ đếm collection trong transaction; sử dụng
giới hạn số attempt/request riêng, không tăng số LLM calls cho Tavily.
Chỉ thêm bộ đếm/ledger tối thiểu cần để enforce giới hạn và audit.
Chi phí Tavily/credits tách khỏi estimated LLM USD; không giả định token pricing áp dụng
cho Tavily và không tuyên bố hard dollar cap. Không ghi nhận 0 USD như chi phí đã biết
khi provider không trả thông tin giá.

## 7. Cấu hình và telemetry

Thêm settings và .env.example, với tên cuối cùng thống nhất trong code/docs:

- TAVILY_API_KEY: SecretStr, không có giá trị mặc định thật.
- TAVILY_REQUEST_TIMEOUT_SECONDS: mặc định 30, kiểm tra giá trị dương.
- TAVILY_MAX_RESULTS: mặc định 5, giới hạn trên hữu hạn.
- COLLECT_MAX_ATTEMPTS: mặc định 2; không vượt trần workflow hiện có.
- Giới hạn text/query policy có version và được đưa vào hash nếu ảnh hưởng output.

Chỉ cần key khi dùng tavily mode; /ready không gọi Tavily để kiểm tra.
Telemetry ghi job_id, step, attempt, latency, số nguồn, loại lỗi và usage nếu có.
Không log key, raw exception/body, nội dung nguồn hoặc query chứa dữ liệu công ty theo mặc định.
Langfuse capture content vẫn theo cơ chế opt-in hiện tại. COLLECT có span riêng,
không bị nhận nhầm là OpenAI generation. URL/query/content trong trace cũng phải
tuân thủ content-capture policy, không lọt qua metadata.

## 8. Checklist triển khai theo thứ tự

- [x] Đọc hướng dẫn repo; ghi nhận tests baseline và code hiện tại liên quan.
- [x] Chốt contract API, version workflow và provenance schema theo đặc tả này.
- [x] Thêm domain contracts/schema/policy, tách thứ tự bước theo job.
- [x] Thêm migration/persistence cần thiết, hỗ trợ đọc và resume legacy job.
- [x] Thêm settings, collector port, Tavily adapter và lifecycle/wiring.
- [x] Tích hợp COLLECT vào dispatch, claim, request audit, checkpoint, hash và recovery.
- [x] Xây effective sources; điều chỉnh Research prompt, evidence validation và Report
      cho workflow mới; giữ Classify/Score hoạt động và giữ hành vi legacy.
- [x] Cập nhật API create/get/events/retry/metrics và canonical idempotency.
- [x] Thêm kiểm thử ở mục 9 và xử lý các lỗi phát hiện được.
- [x] Cập nhật README, tài liệu tiếng Việt, env example và demo payload tavily mode.
- [x] Chạy các check liên quan; rà diff để không có secret/raw provider output.
- [x] Cập nhật trạng thái đặc tả và index; ghi kết quả kiểm tra, giới hạn còn lại.

## 9. Kiểm thử bắt buộc và nghiệm thu

Tests tự động dùng mock tại HTTP/provider boundary, không dùng key thật hoặc dịch vụ trả phí.

- Manual request cũ vẫn tạo bốn bước; không instantiate/call Tavily khi không cần.
- Tavily mode chấp nhận thiếu description, manual mode vẫn từ chối description rỗng.
- Cùng payload/key trả job cũ; payload thay đổi trả 409; kiểm tra default mới với bản lưu legacy.
- Happy path năm bước: nguồn lưu trước Research, có provenance/URL trong kết quả.
- Query độc lập với ICP; domain, dedup, giới hạn text và result count hoạt động đúng.
- Evidence không nằm trong nguồn hoặc tham chiếu source_id giả bị từ chối.
- Nội dung web có câu lệnh không được biến thành instruction cho model; test prompt
  contract không được mô tả là chứng minh tuyệt đối khả năng chống prompt injection.
- Không có kết quả, cấu hình sai, 429, timeout, 5xx và response sai schema theo bảng lỗi.
- Không tăng LLM counter/cost reservation khi chỉ gọi Tavily; giới hạn collection được enforce.
- Crash/resume sau COLLECT checkpoint không gọi search lại; timeout/crash không rõ
  kết quả không tự retry; admin retry tuân thủ trần và khả năng tính phí trùng.
- Hai worker/duplicate message không cùng claim thành công; stale worker không commit.
- Legacy completed/pending/partially completed job đọc và resume đúng; không hash mismatch giả.
- Telemetry mặc định không chứa nguồn/query/key; opt-in capture và span type đúng.
- Kiểm tra migration và recovery trên SQLite và PostgreSQL khi có môi trường hỗ trợ.
  Ghi rõ check nào chưa chạy vì thiếu môi trường, không báo pass thay cho skipped.

Chạy các check hiện có phù hợp: pytest, ruff check, ruff format --check, alembic check.
Không chạy demo OpenAI/Tavily trả phí như một phần tests mặc định; smoke thật cần yêu cầu
rõ ràng của người dùng và phải báo số call/khả năng tính phí trước khi chạy.

Hoàn thành khi cả hai mode chạy đúng, nguồn có thể truy vết, recovery không search lại
checkpoint đã có, job cũ tương thích và các kiểm tra bắt buộc có kết quả được ghi nhận.

## 10. Tài liệu tham khảo khi triển khai

- Tavily API: https://docs.tavily.com/documentation/api-reference/introduction
- Tavily Search: https://docs.tavily.com/documentation/api-reference/endpoint/search
- Workflow hiện tại: `docs/end-to-end.vi.md`.
- Kiến trúc và database: `docs/architecture.vi.md`.
- Correctness/cost policy: `README.md`, `docs/verification.md`.

Đọc lại tài liệu API chính thức khi bắt đầu implementation; không coi tên tham số,
giá hoặc đặc tính retry của provider là cố định theo thời gian.

## 11. Bằng chứng hoàn thành

Triển khai ngày **2026-10-18**. Hướng dẫn vận hành:
[Tavily collection](../docs/tavily-collection.vi.md); payload:
[tavily-company.json](../examples/research/tavily-company.json).

- Hai mode giữ hợp đồng tương thích; workflow `manual-v1` có bốn bước và `tavily-v1`
  có COLLECT đứng đầu. Hash/schema/prompt manual giữ nguyên; canonical comparison
  xử lý default mới trên bản lưu legacy, kể cả request sát giới hạn input.
- Async Tavily REST adapter: một basic Search POST mỗi attempt, URL/text bounded,
  checkpoint bền vững, evidence_sources kiểm tra quote/source, report nguồn và hạn chế.
- Collection intent/usage riêng với LLM, retry 429 hữu hạn, unknown cần admin acknowledge,
  lease/heartbeat/fencing bảo vệ checkpoint. Telemetry không capture content mặc định.
- Migration mới `26a1018_tavily_collection`; chạy `alembic upgrade head` trước khi
  khởi động API/worker. Thêm cấu hình Tavily trong `.env.example`; manual không cần key.
- Baseline trước sửa: `pytest -q --basetemp=data/pytest-f001-baseline`:
  **75 passed, 37 skipped** (các môi trường PostgreSQL/Redis tùy chọn chưa bật).
- SQLite full suite: `pytest -q --basetemp=data/pytest-f001-full`:
  **177 passed, 70 skipped**. Skip thuộc các biến thể PostgreSQL và Redis chưa bật
  ở lượt chạy này; kết quả PostgreSQL thật được kiểm tra riêng.
- PostgreSQL 16 thật trong container test tạm: collection workflow/repository,
  repository/recovery/async invariants **85 passed, 0 skipped**.
- Full suite cuối với PostgreSQL **16.15** và Redis **7.4.11** thật, bật
  `TEST_POSTGRES_URL`, `RUN_REDIS_E2E=1`, `TEST_REDIS_URL` trỏ vào container test tạm:
  `python -m pytest -q --disable-warnings --basetemp data/f001-audit-full-pytest`:
  **247 passed, 0 skipped**, 58,67 giây. Bao gồm cả hai broker E2E cases.
- SQLite và PostgreSQL: migration từ revision cũ lên head, replay legacy pending/
  partially completed/completed, và `alembic check` đều pass; không có schema drift.
  LLM mock cần lần lượt 3/2/0 call khi replay các trạng thái legacy trên PostgreSQL.
- `ruff check .`, `ruff format --check .` (**102 files already formatted**) và
  `git diff --check` đều pass. Container PostgreSQL/Redis test đã được gỡ sau kiểm tra;
  không sửa database hoặc queue ứng dụng.
- Provider boundary đều mock; không gọi Tavily/OpenAI trả phí. Các cảnh báo dependency
  hiện có (CrewAI/Alembic/datetime adapter) được giữ trong output kiểm thử.

Giới hạn: snippet có thể thiếu/cũ/sai; substring provenance không chứng minh sự thật
hoặc bảo đảm tuyệt đối chống prompt injection. Usage/credits chỉ là số provider trả
ở response đã checkpoint; không phải billing ledger hoặc hard USD cap. Request timeout
hoặc crash sau dispatch vẫn có thể đã tính phí, vì vậy không tự retry.
