# 07 — Kiểm thử, xử lý lỗi và giới hạn

Validation hoàn tất ngày **2026-10-18**, Python **3.13.13** trên Windows. Full suite
với SQLite, PostgreSQL **16.15** và Redis **7.4.11** thật: **488 passed, 0 skipped**,
1244 dependency deprecation warnings, 122.24 giây. Không gọi OpenAI/Claude/Ollama/Tavily
thật, không tải model, không paid smoke. PostgreSQL/Redis dùng container tạm riêng;
không dùng database/queue ứng dụng hoặc Redis hiện có của người dùng.

## Những gì đã chạy

| Check | Kết quả thực tế |
|---|---|
| Baseline trước thay đổi: architecture + public API | 3 passed, 2 PostgreSQL skipped vì chưa cấu hình |
| Full default environment sau phase application | 336 passed, 110 optional PostgreSQL/Redis skipped |
| Application + architecture | 16 passed |
| Reader + collection/execution repository regression, hai DB | 61 passed |
| Binding/session/lifecycle/architecture | 12 passed |
| Admin HTTP E2E riêng, hai DB | 20 passed, không skipped |
| Admin HTTP + public API + architecture | 25 passed |
| Full suite, PostgreSQL và Redis test đã bật | **488 passed, không skipped** |
| `ruff check .` | passed |
| `ruff format --check .` | passed |
| `git diff --check` | passed |
| Alembic upgrade head + check, SQLite tạm | no new upgrade operations |
| Alembic upgrade head + check, PostgreSQL database tạm riêng | no new upgrade operations |

Skipped ở các lần đầu không được tính thành pass PostgreSQL/Redis. Lần cuối đã chạy
cả hai backend và broker E2E. F003 không thêm cột/index/migration; các migration
F001/F002 hiện có vẫn cần ở head trước khi chạy bản API này.

Tái lập trên DB/Redis **test cô lập**, không trỏ các biến dưới đây vào production:

```powershell
$env:TEST_POSTGRES_URL = '<async PostgreSQL DSN của database test>'
$env:RUN_REDIS_E2E = '1'
$env:TEST_REDIS_URL = '<Redis URL test riêng>/15'
.\.venv\Scripts\python -m pytest -q --disable-warnings --show-capture=no --tb=short `
  --basetemp=data/pytest-f003-final
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
git diff --check
```

Các fixture PostgreSQL tạo schema ngẫu nhiên rồi chỉ xóa schema mình tạo.
Redis E2E dùng queue tên ngẫu nhiên và dọn queue test; vẫn nên dùng Redis tạm riêng
như lần validation này. Không có seed production lúc startup.

## Coverage theo boundary

[test_admin_service.py](../../tests/unit/test_admin_service.py) kiểm tra caller →
`ResearchAdminService` → port, filters/page thuần Python, UTC/trim, not-found và giới
hạn jobs 100/history 500. Service không nhận publisher/provider. Architecture test
giữ chiều import hướng vào domain/application.

[test_admin_reader.py](../../tests/integration/test_admin_reader.py) dùng session/DB
thật: list projection nhẹ, số query không tăng theo số items, tie order, literal
`%`/`_`/`\`, Unicode tiếng Việt, UTC hai đầu, total/page ngoài phạm vi; detail detached,
deepcopy JSON, legacy/hỏng config, partial COLLECT, safe metadata/errors/events,
nullable usage, metrics chung và toàn bộ rows không đổi.

[test_admin_dependencies.py](../../tests/integration/test_admin_dependencies.py)
kiểm tra bootstrap binding, construction không phát SQL, service nhận reader thay thế.
[test_dependency_injection.py](../../tests/integration/test_dependency_injection.py)
giữ session-sharing trong request, rollback và concurrency/idempotency hiện tại.

[test_admin_api.py](../../tests/integration/test_admin_api.py) là HTTP→ASGI→lifespan→
production service/reader→DB thật. Caller gửi Bearer và query, nhận response JSON;
chỉ database/publisher boundary được fixture thay thế. Không mock service để gọi đó
là E2E. Các case bao phủ 401 thiếu/sai/rỗng token và header Unicode, auth không đọc
port, 422 trước reader method, 404 UUID không tồn tại, empty history, bounded pages,
filter/order, safe detail/history, null khác 0, OpenAPI Bearer, session riêng từng
request, contract override không cần session và GET chỉ SELECT/không enqueue/không
sửa row/timestamp/status/lease. JSON vẫn đọc được sau khi dependency đóng session.

Full suite còn giữ public create/get/events/retry, F001/F002 routing/collection,
recovery/concurrency, actual Crew với mocked provider và Redis/Celery E2E.

## Chẩn đoán theo input → method → output

| Hiện tượng | Kiểm tra cụ thể |
|---|---|
| 401 ADMIN_AUTH_REQUIRED | Token cấu hình có rỗng không; API đã restart/recreate sau đổi env chưa; header chính xác `Bearer <token>`. Swagger nhập chỉ token. Không log header. |
| 404 JOB_NOT_FOUND | UUID có thật trong đúng database/backend của API không; list cùng server để lấy ID. Missing history khác existing-empty. |
| 422 | Xem validation details: UUID, status, limits, tên sau trim, ISO có timezone, range và query dư. Encode dấu `+` trong timezone. UNKNOWN không phải job status. |
| 200 items=[] | Bình thường nếu chưa có job/history, filter không khớp hoặc offset vượt cuối; xem total và thử offset=0. Không tạo paid job chỉ để lấp trang. |
| Metadata null | Legacy job chưa lưu config hoặc snapshot không validate được. Adapter không tự dùng settings hôm nay để gán metadata cũ. |
| Token/cost/credits null | Chưa có measurement, không phải 0 hoặc miễn phí. Xem attempt status/request-start và policy reservation. |
| UNKNOWN_ERROR | Code lịch sử không thuộc category nhận biết; raw message bị ẩn có chủ ý. Dùng trạng thái, event type, ID và thông tin vận hành đã sanitize để điều tra. |
| Thiếu event | Retention có thể xóa lịch sử; cleanup hiện tại chỉ xóa event cũ của completed jobs. Event lạ có details={} theo allowlist. |
| `Research operation requires an idle session` | Đang lồng operation hoặc giữ transaction cũ; dùng request session và scope rollback của adapter, không gọi đồng thời trên cùng session. |
| SQLite thiếu `unicode_lower` | Session dùng engine tự tạo, bỏ qua hook của `Database`. Dùng factory production hoặc override database đúng cách. |
| Session/pool chưa trả connection | Kiểm tra yield dependency, context manager và caller giữ session ngoài scope; DTO phải serialize được sau khi đóng session. |
| DB/SQL lỗi | Kiểm tra backend/DSN riêng tư, migration head và connection; response production mặc định không chứa SQL/stack trace. Không bật log raw company data để debug. |
| `/ready` 503 do Redis | Readiness kiểm tra cả broker, khác admin GET vốn chỉ cần DB. Không sửa Redis/queue chỉ vì trang admin trống. |

Ví dụ Sen: list trả NEEDS_REVIEW → `get_job_detail(id)` thấy CLASSIFY UNKNOWN →
events/attempts cho biết thời điểm/request-start/error đã sanitize. Đây là luồng đọc;
không tự chuyển UNKNOWN về PENDING, không sửa lease và không enqueue retry.

## Giới hạn cần hiểu đúng

Offset pagination có thể dịch trang khi có insert/update; count/items không được
cam kết cùng snapshot tuyệt đối ở mọi isolation level. Substring search phân biệt
dấu; SQLite dùng Unicode lower của Python, PostgreSQL theo locale nên vài Unicode
đặc biệt có thể khác. Không phải tìm kiếm bỏ dấu hoặc full-text search.

Shared ADMIN_TOKEN không nhận diện từng nhân viên. Public research auth không tự
thay đổi. Detail giữ input/output công ty do người dùng cung cấp; không đưa secret
vào research input, không log/share dữ liệu này tùy tiện. Event allowlist bảo vệ
details lịch sử, không phải cam kết mọi company text đều không chứa credential.

Usage và USD là số đã lưu/ước lượng theo policy hiện có, không phải billing ledger;
COLLECT credits là đơn vị riêng. UNKNOWN có thể đã phát sinh phí. Nếu cần thao tác
retry, xem [05-api.md](05-api.md) và [hướng dẫn recovery](../end-to-end.vi.md), cân nhắc
acknowledgement/phí trùng và domain restrictions. Không có retry mới hoặc mutation
admin trong F003. Thực tế chất lượng/model access/provider billing không được xác
nhận bằng các test mock, và không cần xác nhận chúng để xem admin job có sẵn.
