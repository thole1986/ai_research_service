# F003 — Admin research API end to end và tài liệu DDD từng layer

- Trạng thái: IMPLEMENTED — 2026-10-18.
- Ngày tạo: 2026-10-17.
- Phạm vi người dùng xác nhận: viết đặc tả trước; API qua Swagger/Postman, không dashboard.
- Yêu cầu bắt buộc khi triển khai: sau mỗi layer hoàn thành, viết tài liệu giải thích
  layer đó trong `docs/admin/` trước khi chuyển sang layer tiếp theo.
- F001/F002 không phải dependency bắt buộc. Đối chiếu chúng nếu code đã được triển khai.

## 1. Mục tiêu

Người dùng nội bộ có thể xem tổng quan hệ thống, tìm research job, xem trạng thái,
kết quả, các bước, lịch sử attempt và sự kiện qua admin API được bảo vệ bằng token.
Mọi thao tác xem dữ liệu không được enqueue job, gọi provider hoặc phát sinh LLM call.
Tài liệu giúp người mới hiểu code thực sự hoạt động theo DDD/DI/async ra sao và tự
gọi được từng endpoint trong Swagger hoặc Postman.

Không thêm dashboard, tạo tài khoản, RBAC, SSO, sửa/xóa job, cancel/retry hàng loạt,
export hay endpoint thay đổi cấu hình. Không thêm retry mới trong F003; admin xem lý
do cần review và đọc hướng dẫn dùng endpoint retry hiện có khi đủ điều kiện.
Không tuyên bố shared ADMIN_TOKEN là hệ thống đăng nhập hay audit theo từng nhân viên.

## 2. Hiện trạng cần đọc lại khi triển khai

| File hiện tại | Vai trò |
|---|---|
| `src/company_ai/api/admin/router.py` | Router /api/v1/admin hiện chỉ có GET /metrics |
| `src/company_ai/api/dependencies.py` | require_admin, token Bearer so sánh bằng compare_digest |
| `src/company_ai/api/research/dependencies.py` | Contract dependency và cách tạo JobService |
| `src/company_ai/application/research/jobs.py` | Use cases create/get/events/retry/metrics hiện tại |
| `src/company_ai/application/research/ports.py` | Repository port, publisher port |
| `src/company_ai/domain/research/entities.py` | Aggregate/snapshot ResearchJob, ResearchStep, JobEvent |
| `src/company_ai/domain/research/contracts.py` | Các bước và claim/result |
| `src/company_ai/infrastructure/research/persistence/models.py` | ORM Job, Step, Attempt, Event |
| `src/company_ai/infrastructure/research/persistence/repositories.py` | Query/transaction hiện tại |
| `src/company_ai/infrastructure/research/persistence/mapping.py` | ORM → snapshot, deepcopy |
| `src/company_ai/bootstrap/dependencies.py` | Gắn port với implementation và AsyncSession |
| `src/company_ai/main.py` | App factory, global JobNotFound handler, middleware |
| `tests/unit/test_architecture.py`, `tests/conftest.py` | Hướng import và dependency override tests |

Đọc AGENTS.md áp dụng trước khi sửa. Kiểm tra trạng thái job/step từ code hiện tại,
không invent status mới để phục vụ bộ lọc. Hiện API research đã có get job/events
nhưng không có list jobs/attempt history cho admin.

## 3. Ranh giới DDD — quyết định thiết kế

Admin hiện là góc nhìn quản trị của bounded context research, chưa phải một domain
có nghiệp vụ độc lập. Không tạo `domain/admin` chỉ để sao chép ResearchJob.

Tổ chức dự kiến:

```text
domain/research/
  entities.py                 # Giữ aggregate/snapshot hiện tại
  ...                         # Chỉ thêm business concept thật nếu cần
application/research/
  admin_queries.py            # Filter, paging, read DTOs
  admin_ports.py              # ResearchAdminReader protocol
  admin_service.py            # Read use cases cho admin
infrastructure/research/persistence/
  admin_reader.py             # SQLAlchemy implementation, ORM không thoát ra ngoài
api/admin/
  router.py                   # HTTP, auth, gọi use case, response mapping
  schemas.py                  # Pydantic request/query/response contract
  dependencies.py             # Contract provider và Admin service factory
bootstrap/dependencies.py     # Binding concrete adapter, dùng get_session sẵn có
docs/admin/                   # Tài liệu theo từng layer
```

Tên file có thể điều chỉnh nếu repo đã thay đổi; giữ ranh giới và cập nhật tài liệu
theo tên cuối cùng. Không thêm BaseRepository CRUD chung cho nhu cầu đọc này.

### Quy tắc phụ thuộc

- Domain: không FastAPI, SQLAlchemy, Pydantic HTTP schema hoặc provider SDK.
- Application: use case/DTO/port; không import API/infrastructure/SQLAlchemy.
- Infrastructure: implement port, query ORM, ánh xạ thành read DTO/snapshot detached.
- API: validate HTTP, auth, gọi application, map response; không viết SQL.
- Bootstrap: nơi biết cả port và implementation; gắn dependencies.
- Read models có thể nằm ở application vì phục vụ truy vấn quản trị, không cần biến
  mọi row tổng hợp thành aggregate domain. Đây là tách read side đơn giản, không phải
  CQRS/event sourcing đầy đủ và không có database thứ hai.
- Không chuyển policy retry/lease/budget vào router hoặc tạo policy bản sao cho admin.

## 4. API contract v1

Tất cả endpoint dưới `/api/v1/admin` dùng require_admin hiện tại.
Thiếu/sai token hoặc ADMIN_TOKEN rỗng → 401 ADMIN_AUTH_REQUIRED như hiện nay.
Các endpoint chỉ đọc trả 200, job không tồn tại → 404 JOB_NOT_FOUND;
query/path không hợp lệ → 422. Không để lỗi SQL hoặc stack trace ra response.

| Method/path | Mục đích |
|---|---|
| GET /metrics | Giữ contract hiện tại, thêm response schema rõ ràng |
| GET /jobs | Danh sách tóm tắt, filter và phân trang |
| GET /jobs/{job_id} | Thông tin job, input, steps và result |
| GET /jobs/{job_id}/events | Sự kiện audit đã sanitize, có phân trang |
| GET /jobs/{job_id}/attempts | Lịch sử attempt của các bước, có phân trang |

Không thay đổi route public research hoặc làm chúng tự nhiên được bảo vệ bởi admin
token trong feature này; giải thích ranh giới auth hiện tại trong docs. Không gọi router
research từ router admin như một hàm để tái sử dụng logic HTTP.

### 4.1 GET /jobs

Query parameters:

- status: một job status hợp lệ, tùy chọn, so khớp chính xác.
- company_name: tìm substring không phân biệt hoa/thường, 1–200 ký tự sau trim.
  Escape `%`, `_` như ký tự thường; dùng bind parameters, không nối chuỗi SQL.
- created_from, created_to: ISO 8601 có timezone, chuyển về UTC; khoảng bao gồm hai đầu.
  Reject datetime không timezone hoặc from > to.
- offset: mặc định 0, >=0; limit: mặc định 50, từ 1 đến 100.
- Thứ tự cố định created_at DESC, id DESC; không nhận arbitrary SQL sort expression.

Response: `{items: [...], total: int, offset: int, limit: int}`.
Item chỉ có job_id, company_name, website, status, created_at, updated_at, completed_at,
estimated_cost_usd, error_code. Không tải/list toàn bộ input, result hay step outputs.
total áp dụng cùng filters. Trang vượt cuối → items=[] với total đúng.
Offset pagination không bảo đảm snapshot bất biến khi có job mới; ghi rõ giới hạn.

### 4.2 GET /jobs/{job_id}

Trả thông tin tương đương JobView hiện tại, thêm input và thông tin phục vụ xem tiến
trình an toàn: step_name/status/attempt_count/timestamps/next_retry_at/lease_expires_at
nếu cần diễn giải. Giữ result và output mỗi step để đọc RESEARCH/CLASSIFY/SCORE/REPORT,
COLLECT nếu F001 đã có. F002 có metadata provider/model thì dùng dữ liệu đã lưu.
Không trả lease_token, admin token, API keys, endpoint nội bộ, request hash hoặc
idempotency key; chúng không cần cho nhu cầu xem nội bộ này.

Input và outputs có thể chứa dữ liệu công ty, chỉ trả tại endpoint detail đã auth;
không log chúng. Không cam kết raw input của người dùng luôn không chứa secret;
tài liệu hướng dẫn không đưa credential vào nội dung nghiên cứu.

Không suy ra review/retry eligibility bằng logic riêng ở API. V1 trả state/error
và tài liệu giải thích; nếu thêm eligibility sau này phải gọi policy domain hiện có.

### 4.3 GET /jobs/{job_id}/events

Query offset=0, limit=100 (1–500), event_type tùy chọn (exact match, giới hạn chiều dài).
Trả envelope items/total/offset/limit. Mỗi item: id, step_id, event_type, details, created_at.
Order created_at ASC, id ASC để đọc timeline; total cùng filter.
Kiểm tra job tồn tại trước; job không có event trả trang rỗng, không 404.
Details dùng allowlist field an toàn từ code emit event; không phơi bày arbitrary
provider error, raw body hoặc secret nếu lịch sử có dữ liệu ngoài dự kiến.
Event retention có thể khiến lịch sử không đầy đủ; docs phải nói rõ.

### 4.4 GET /jobs/{job_id}/attempts

Query offset=0, limit=100 (1–500), step_name và status tùy chọn, validate theo contract
thực tế. Order created_at ASC, id ASC; envelope items/total/offset/limit.
Mỗi item: attempt_id, step_id, step_name, attempt_no, status, error_type,
created_at, llm_started_at, finished_at, tokens_input/output, estimated_cost_usd,
provider_request_id nếu đang lưu và an toàn cho operator.
Không trả lease_token; không tải raw provider response/prompt từ nơi khác.
Nếu F001/F002 đã thêm fields: thêm execution_type/provider/model/pricing kind/collection
usage từ dữ liệu lưu thật, không gán giả khi thiếu. Viết rõ null khác 0.

### 4.5 GET /metrics

Giữ keys/types hiện tại: jobs, steps, events (count theo trạng thái/loại), llm_calls,
tokens_input, tokens_output, estimated_cost_usd. Response schema typed.
Không tính lại token/cost từ result, không gọi provider. Giá là ước lượng theo policy
hiện có, không hóa đơn. Nếu F001/F002 đã thêm breakdown, bảo toàn contract đó.

## 5. Application use cases và repository port

`ResearchAdminService` nhận `ResearchAdminReader`, không nhận JobPublisher.
Use cases: list_jobs, get_job_detail, list_job_events, list_job_attempts, get_metrics.
Filter/page DTO dùng frozen dataclass hoặc type thuần có validation phù hợp;
không truyền đối tượng FastAPI Request/Query, Pydantic response hoặc ORM vào application.

Port trả DTO/snapshot detached và Page có items,total,offset,limit. Cần bảo đảm job
tồn tại cho events/attempts; tái sử dụng application JobNotFound hoặc error chung phù
hợp, tránh exception đồng nghĩa ở nhiều module. API map 404 nhất quán.
Admin service chỉ đọc, không gọi JobService.create/retry hoặc provider adapter.

Metrics reader có thể tái sử dụng truy vấn chung ở infrastructure hoặc extract helper
phù hợp; không copy hai bản SQL metrics dễ lệch. Giữ compatibility cho service/tests cũ.

## 6. Infrastructure, query và transaction

- Implement reader với AsyncSession do request dependency cấp; không dùng global session.
- Dùng ORM/query parameterization, count/filter giống nhau cho items và total.
- Company name nằm trong input_json: hỗ trợ JSON extraction trên cả SQLite/PostgreSQL;
  viết tests hai backend khi có môi trường, không dùng SQL chỉ chạy PostgreSQL.
- Không eager load toàn bộ jobs/attempts để phân trang bằng Python.
- Không query một lần cho từng item (N+1); detail lấy job/steps theo số query hữu hạn.
- Ánh xạ dữ liệu trước khi trả ra khỏi session; không trả ORM, lazy relationship hoặc
  object yêu cầu session còn mở để serialize.
- Read không gọi FOR UPDATE, không sửa updated_at/status/lease hoặc mở write transaction.
- Không giữ session/transaction trong thời gian gọi network; feature này không cần network.
- Items và total được đọc trong transaction/session ngắn. Không hứa total phản ánh
  cùng snapshot tuyệt đối trên mọi isolation level khi worker đang cập nhật.
- Đánh giá query plan trước khi thêm index. Không cần migration cho DTO/API thuần;
  nếu thêm index thật thì Alembic revision mới và kiểm tra backend tương ứng.

## 7. Bootstrap/DI và API

`api/admin/dependencies.py` khai báo get_admin_reader (contract provider) và
get_admin_service(reader) → ResearchAdminService, kèm Annotated alias dễ đọc.
Bootstrap thêm get_sqlalchemy_admin_reader(session, ...) và đăng ký override contract
provider trong configure_dependencies giống research. Không import infrastructure vào API.

Luồng cần hiện thực và giải thích bằng tên code cuối cùng:

```text
GET /api/v1/admin/jobs?status=NEEDS_REVIEW
  → require_admin kiểm tra token
  → router nhận query đã validate
  → Depends(get_admin_service)
      → Depends(get_admin_reader)                 [port]
          → bootstrap get_sqlalchemy_admin_reader [implementation]
              → Depends(get_session)              [AsyncSession theo request]
  → AdminService.list_jobs(filter, page)
  → SqlAlchemyAdminReader.list_jobs(...)
  → DB → detached Page[JobSummary]
  → router map Pydantic response → JSON
  → dependency đóng session
```

Đồ thị DI mô tả việc resolve dependency; không được diễn giải thành call stack SQL
đã chạy trong constructor. Repo chỉ query khi use case gọi method.
FastAPI cache dependency trong request không đồng nghĩa session singleton toàn app.
Auth và query handling phải hoàn tất trước data query; tests chứng minh token sai
không đọc repository. OpenAPI khai báo Bearer security để Swagger Authorize dùng được;
giữ tương thích header Authorization hiện tại và error code 401 khi điều chỉnh dependency.
Không bật persistAuthorization mặc định hoặc nhúng token trong URL/example thật.

## 8. Tài liệu bắt buộc trong docs/admin/

Tài liệu là một phần của tiêu chí hoàn thành, không gom viết sơ sài sau khi code xong.
Các file dưới đây được tạo khi triển khai layer tương ứng; hiện chưa tạo tài liệu giả
khẳng định code F003 đã tồn tại.

| File | Nội dung bắt buộc | Viết khi nào |
|---|---|---|
| README.md | Mục tiêu, link đọc theo thứ tự, trạng thái, sơ đồ tổng quan | Bắt đầu implementation, cập nhật mỗi phase |
| 01-domain.md | Bounded context research, aggregate vs read DTO, policy hiện có, vì sao chưa cần domain/admin | Sau rà soát/chỉnh domain |
| 02-application.md | Use cases, port, DTO/filter/Page, ai gọi ai, không import SQLAlchemy | Sau application |
| 03-infrastructure.md | SQLAlchemy reader, query/count/paging, mapping, session/transaction | Sau infrastructure |
| 04-bootstrap-di.md | Contract dependency, override binding, lifespan/session, cách test override | Sau wiring |
| 05-api.md | Mọi endpoint/query/response/error/auth, Swagger và Postman | Sau API |
| 06-end-to-end.md | Hai walkthrough list/detail với dữ liệu mẫu, call chain và debug | Sau tích hợp E2E |
| 07-testing-troubleshooting.md | Check đã chạy, auth/404/422/empty page, DB/session/DI, giới hạn | Sau validation |

Mỗi tài liệu layer phải có:

1. Layer giải quyết việc gì, giải thích tiếng Việt thông thường trước thuật ngữ.
2. Tên file/class/function thực tế và links tương đối tới source; không dùng line number
   dễ lỗi thời, không dùng pseudo filename nếu implementation khác đặc tả.
3. Caller → method → input → output cụ thể. Với layer không có HTTP endpoint, minh họa
   bằng đoạn code thật hoặc rút gọn chính xác cách layer trên gọi nó.
4. Một ví dụ xuyên suốt cùng job giả định, phân biệt dữ liệu lưu DB và JSON response.
5. Lý do đặt logic ở đó và dependency nào được phép/cấm, với ví dụ ngay trong repo.
6. Những điểm dễ nhầm: port không phải implementation, Depends không tự chạy SQL,
   ORM khác domain snapshot, async không tự tạo worker, API đọc không gọi LLM.
7. Cách kiểm tra/debug thực tế và test liên quan; diễn giải không chỉ dán code dài.

Domain doc phải nói rõ nếu layer này không cần thêm code vì feature chỉ là read use
case; không invent entity/policy để đủ bốn layer. End-to-end docs có Mermaid sequence
diagram và giải thích theo từng mũi tên, phân biệt chiều import với chiều gọi runtime.
Giải thích `await`, lifespan, session scope, dependency cache bằng ví dụ cụ thể.

Hướng dẫn HTTP dùng PowerShell UTF-8 và Postman:

- Cấu hình ADMIN_TOKEN trong env, restart service theo tài liệu hiện có.
- Swagger Authorize bằng bearer token, gọi metrics/list/detail/events/attempts.
- Postman đặt Authorization → Bearer Token; không gửi token vào query string.
- Ví dụ Invoke-RestMethod lấy token từ biến môi trường, dùng placeholder nếu chưa có;
  không ghi token thật vào file, ảnh hoặc output.
- List → lấy job_id → detail → events → attempts, giải thích từng response/error.
- Hướng dẫn đọc NEEDS_REVIEW/UNKNOWN, missing evidence và estimated cost.
- Nếu cần retry, link endpoint hiện có, giải thích khả năng tính phí trùng và domain
  restrictions; tutorial không tự gọi retry như bước xem dữ liệu.
- Không yêu cầu gọi paid LLM để xem admin: dùng job có sẵn hoặc fixtures test; nếu DB
  rỗng giải thích items=[] là bình thường. Không thêm seed production khi startup.

## 9. Checklist triển khai, có tài liệu sau mỗi layer

- [x] Đọc hướng dẫn/code hiện tại, ghi baseline và trạng thái F001/F002.
- [x] Tạo docs/admin/README.md với scope, lộ trình và trạng thái chưa hoàn thành.
- [x] Rà domain boundaries/policy; chỉ sửa khi cần → viết 01-domain.md.
- [x] Application DTOs/ports/use cases, unit tests → viết 02-application.md.
- [x] Infrastructure reader/mapping/query tests → viết 03-infrastructure.md.
- [x] Bootstrap/DI/lifecycle và dependency override tests → viết 04-bootstrap-di.md.
- [x] API schemas/router/auth/OpenAPI, HTTP tests → viết 05-api.md.
- [x] E2E HTTP→DB thật qua ASGI fixtures, không provider calls → viết 06-end-to-end.md.
- [x] Chạy validation/review auth/data exposure/query bounds → viết 07-testing-troubleshooting.md.
- [x] Kiểm tra links, ví dụ, sơ đồ đúng tên code cuối cùng; cập nhật docs/admin/README.md.
- [x] Link docs/admin/ từ README/tài liệu tiếng Việt phù hợp.
- [x] Cập nhật features index/F003 status và bằng chứng hoàn thành.

Trong tiến trình implementation, cập nhật ngắn cho người dùng layer vừa hoàn thành,
file docs tương ứng và bước tiếp theo. Không báo hoàn thành layer chỉ vì đã tạo file rỗng.

## 10. Kiểm thử bắt buộc

- Unit: application gọi đúng port, filters/page DTO, not found, không publisher/provider.
- Architecture: test import direction hiện có vẫn pass; adapter trả DTO không ORM.
- HTTP auth: mọi route mới và metrics trả 401 khi thiếu/sai/rỗng token; không repository query
  khi auth thất bại. OpenAPI Bearer security hoạt động cho Swagger.
- List: empty/full/page beyond end, total cùng filters, bounded pagination, stable tie order.
- Filter: exact status, literal `%`/`_`, Unicode tên công ty, timezone conversion, invalid
  ranges/naive dates/unknown status bị 422. Không tuyên bố tìm kiếm bỏ dấu tiếng Việt.
- Detail: input/result/steps đúng, job 404, không leak token/lease/idempotency key/endpoint.
- Events/attempts: phân trang/filter/order đúng, job tồn tại nhưng không history khác job 404;
  null usage khác 0; details allowlist và error sanitization.
- Metrics contract hiện tại giữ nguyên; tổng từ DB đúng với fixtures, không provider call.
- Read-only invariant: gọi toàn bộ GET không sửa row/status/timestamps/lease, không enqueue.
- Integration dùng DB thật SQLite qua HTTP ASGI + lifespan và override session phù hợp;
  không chỉ mock service rồi gọi đó là end to end. PostgreSQL khi có môi trường.
- Hai request không dùng chung session ngoài scope; response serialize được sau đóng session.
- Query không N+1; page không tải toàn DB; tests tập trung hành vi cần bảo đảm.
- Regression create/get/events/retry public, require_admin và DDD architecture hiện có.
- F001/F002 nếu đã implement: admin xem được steps/metadata mới, legacy job không lỗi.

Chạy pytest liên quan và suite hồi quy phù hợp, ruff check, ruff format --check;
alembic check nếu có thay schema/index hoặc môi trường hỗ trợ. Không cần paid smoke.
Ghi rõ PostgreSQL/Redis check chưa chạy do thiếu môi trường, không coi skipped là pass.

## 11. Tiêu chí nghiệm thu

API dùng qua Swagger/Postman cho phép metrics → list/filter → detail → events/attempts;
tất cả admin endpoints được bảo vệ. Luồng query đúng ranh giới DDD, không gọi AI
hoặc thay đổi workflow. Contract cũ giữ tương thích và tests có kết quả thực tế.

docs/admin/ có đủ giải thích từng layer ngay sau phase tương ứng và walkthrough
end-to-end cuối cùng với links/code examples đúng. Người mới có thể lần theo một
request từ HTTP tới DB và trở về mà không phải tự đoán binding hoặc session lifecycle.
Không có dashboard hoặc chức năng mutation ngoài scope.

## 12. Bằng chứng hoàn thành

Hoàn thành ngày **2026-10-18**. F001/F002 đã được triển khai trước F003; admin giữ được
COLLECT, partial results, provider/model/pricing metadata đã lưu và legacy jobs.

Năm endpoint được bảo vệ bằng Bearer: GET `/api/v1/admin/metrics`, `/jobs`,
`/jobs/{job_id}`, `/jobs/{job_id}/events`, `/jobs/{job_id}/attempts`.
Application có `admin_queries.py`, `admin_ports.py`, `admin_service.py`; infrastructure
có `admin_reader.py`, `admin_mapping.py`, metrics helper chung; API có schemas và
dependency contracts, bootstrap binding request-scoped AsyncSession. Không sửa domain
policy, thêm dashboard hay mutation. GET không enqueue/gọi provider hoặc thay đổi rows.

Tài liệu đã được viết sau từng phase:
[Tổng quan](../docs/admin/README.md), [Domain](../docs/admin/01-domain.md),
[Application](../docs/admin/02-application.md),
[Infrastructure](../docs/admin/03-infrastructure.md),
[Bootstrap/DI](../docs/admin/04-bootstrap-di.md), [API](../docs/admin/05-api.md),
[End to end](../docs/admin/06-end-to-end.md),
[Testing/troubleshooting](../docs/admin/07-testing-troubleshooting.md).

Baseline scoped: **3 passed, 2 skipped**. Validation cuối trên Python 3.13.13 với
SQLite, PostgreSQL **16.15**, Redis **7.4.11** tạm cô lập:

```text
python -m pytest -q --disable-warnings --show-capture=no --tb=short --basetemp=data/pytest-f003-final
488 passed, 1244 warnings in 122.24s (0:02:02); 0 skipped
```

Ruff check, Ruff format --check, git diff --check đạt. Alembic upgrade head/check
trên SQLite và PostgreSQL tạm riêng đều báo không có upgrade operations mới.
Không thêm migration/index; không paid smoke hoặc gọi provider thật. HTTP E2E dùng
production graph qua ASGI/lifespan và DB thật, kiểm tra auth/422/404, paging/filter,
history allowlist, null/zero, session isolation, lightweight queries và read-only rows.

Cần restart/recreate **API** để nhận routes mới; cấu hình ADMIN_TOKEN nếu chưa có,
dùng Swagger Authorize hoặc Postman Bearer. Không thay provider settings cho F003.
Giới hạn còn lại: shared token không có danh tính/RBAC; public research auth giữ
nguyên; offset/count không hứa snapshot bất biến; event retention có thể mất history;
Unicode đặc biệt phụ thuộc locale; cost là ước lượng, null usage khác 0. Input/output
detail giữ company text nên người dùng không đưa credential vào nội dung nghiên cứu.
