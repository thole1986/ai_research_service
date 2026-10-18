# 05 — API: gọi bằng Swagger, Postman và PowerShell

Layer HTTP nhận query, kiểm tra quyền truy cập, gọi use case và đổi DTO thành JSON.
[router.py](../../src/company_ai/api/admin/router.py) không có SQL hoặc provider call;
[schemas.py](../../src/company_ai/api/admin/schemas.py) định nghĩa query/response
Pydantic. Caller HTTP → router → `ResearchAdminService` → read DTO → response schema.
Ví dụ công ty Sen được lưu ở DB với input/result đầy đủ, nhưng list chỉ trả summary.

## Cấu hình và auth

Đặt `ADMIN_TOKEN` riêng tư trong `.env` theo [hướng dẫn setup](../README.vi.md).
Không commit token, đặt token trong URL hay dán vào ảnh/log. Sau khi đổi `.env`,
recreate API để nhận cấu hình: `docker compose up -d --build --force-recreate api`.
Nếu chạy uvicorn trực tiếp, dừng và chạy lại trong môi trường có token mới.
F003 không yêu cầu đổi cấu hình provider và không có migration mới.

[require_admin](../../src/company_ai/api/dependencies.py) dùng `HTTPBearer` để khai
báo OpenAPI; so sánh raw `Authorization: Bearer <ADMIN_TOKEN>` bằng compare_digest
trên bytes để header Unicode sai cũng trả 401. Scheme và khoảng trắng giữ hành vi
header hiện tại. Thiếu/sai token hoặc token cấu hình rỗng đều trả
`{"detail":"ADMIN_AUTH_REQUIRED"}`, status 401, `WWW-Authenticate: Bearer`.
Auth chạy trước khi đọc repository. Token dùng chung chỉ là quyền truy cập nội bộ,
không phải hệ thống đăng nhập, RBAC hay audit theo từng nhân viên.

Tất cả năm GET dưới `/api/v1/admin` được bảo vệ. Route public research create/get/events
vẫn có ranh giới auth cũ; thêm admin API không tự bảo vệ chúng. Retry research hiện
có vẫn cần Bearer token. Chỉ dùng hệ thống trong phạm vi operator tin cậy như tài liệu
hiện tại; không suy ra rằng token admin bảo vệ toàn bộ service.

## Contract endpoint

Base URL ví dụ: `http://localhost:8000/api/v1/admin`. Mỗi GET thành công trả 200.

| Endpoint | Query | Response |
|---|---|---|
| `/metrics` | không có | `MetricsResponse` |
| `/jobs` | status, company_name, created_from, created_to, offset, limit | `PageResponse[JobSummaryResponse]` |
| `/jobs/{job_id}` | không có; ID UUID | `JobDetailResponse` |
| `/jobs/{job_id}/events` | event_type, offset, limit | `PageResponse[EventResponse]` |
| `/jobs/{job_id}/attempts` | step_name, status, offset, limit | `PageResponse[AttemptResponse]` |

List jobs mặc định `offset=0`, `limit=50`, tối đa 100. `status` exact một trong
PENDING/RUNNING/COMPLETED/NEEDS_REVIEW/FAILED. `company_name` trim rồi dài 1–200 ký tự,
tìm substring không phân biệt hoa thường, giữ nguyên dấu; `%`, `_`, `\` là ký tự
thường. Không có arbitrary sort; thứ tự `created_at DESC, id DESC`.
`created_from`/`created_to` phải là ISO 8601 có timezone, ví dụ
`2026-10-18T07:00:00+07:00`, chuẩn hóa UTC và bao gồm hai đầu. Ngày không timezone,
epoch numeric hoặc from > to bị 422. Query dư cũng bị 422.

Mỗi summary chỉ có `job_id`, `company_name`, `website`, `status`, `created_at`,
`updated_at`, `completed_at`, `estimated_cost_usd`, `error_code`. List không đọc cả
input/result/step output. Envelope `items/total/offset/limit`; total dùng cùng filter,
trang vượt cuối vẫn trả đúng total và items rỗng. Offset không giữ snapshot cố định
khi worker cập nhật hoặc có job mới.

Detail có `input`, `result`, `workflow_version`, trạng thái, timestamps, USD estimate,
error an toàn, các steps và `execution_metadata`. Mỗi step có trạng thái, attempt_count,
output, started/completed/updated/next_retry/lease expiry/heartbeat và metadata nếu có.
Thứ tự COLLECT (nếu có), RESEARCH, CLASSIFY, SCORE, REPORT. Job Tavily có partial
result từ completed checkpoints khi final result chưa có. Metadata lấy từ config đã
lưu: provider/model/profile_id/pricing_kind, không lấy cấu hình hiện tại để điền job cũ.
Config legacy/hỏng có metadata null. Không trả token, hash/key, endpoint hay connection ref.
Error message là câu cố định; error code không nhận biết trở thành UNKNOWN_ERROR.

History mặc định `offset=0`, `limit=100`, tối đa 500; `created_at ASC, id ASC`.
Events exact `event_type` dài 1–80, response mỗi item có id/step_id/event_type/details/created_at.
Details chỉ có field theo allowlist và kiểu an toàn; event lạ có details rỗng. Retention
có thể đã xóa event cũ nên timeline không nhất thiết đầy đủ.

Attempts lọc `step_name` theo ALL_STEPS, status theo
CLAIMED/REQUEST_STARTED/SUCCEEDED/FAILED/UNKNOWN. Item có attempt_id/step_id/step_name,
attempt_no/status/error_type, created_at/llm_started_at/finished_at, tokens_input/output,
estimated_cost_usd/provider_request_id, execution_type/provider/model/pricing_kind,
collection_started_at/collection_usage. Loại execution phản ánh bước: collection,
deterministic, llm; không chứng minh provider đã nhận request. Null usage nghĩa là
chưa biết, khác số 0 đã lưu. COLLECT credits tách khỏi USD LLM; không suy ra miễn phí.

Metrics giữ keys `jobs`, `steps`, `events` (count theo status/type), `llm_calls`,
`tokens_input`, `tokens_output`, `estimated_cost_usd`, `llm_usage` (provider/model/pricing
breakdown), `collection_calls`, `collection_usage`, `collection_credits`. Số liệu lấy
từ DB; USD là ước lượng theo policy hiện có, không phải hóa đơn hoặc hard billing cap.
Usage thiếu có thể null ở breakdown/credits; tổng tokens contract cũ dùng 0 khi chưa
có số liệu. Request-start audit là ý định gọi, không chứng minh đã chạy/đã tính phí.

ID hợp lệ nhưng job không có → 404 `{"detail":"JOB_NOT_FOUND"}` cho detail/events/attempts.
ID không phải UUID, filter/page sai → 422 với FastAPI validation details.
Job có thật nhưng chưa có history → 200 trang rỗng. Empty database cũng bình thường.
Lỗi server không trả SQL/stack trace trong production response mặc định.

## Swagger và Postman

Mở `/docs`, nhấn **Authorize**, nhập chỉ giá trị ADMIN_TOKEN vào scheme **AdminBearer**;
Swagger tự thêm `Bearer`. Không nhập thêm chữ Bearer vào ô token. Gọi lần lượt
metrics → jobs → lấy job_id → detail → events → attempts. Swagger không bật lưu
authorization qua phiên trình duyệt mặc định. Không cần tạo job mới hoặc chạy AI.

Postman: tạo GET với các URL trên, chọn **Authorization → Bearer Token**, đặt token
qua biến môi trường local/private `{{adminToken}}`. Dùng tab Params cho filters,
không đặt token ở Params. Với timezone `+07:00`, để client encode dấu `+` thành `%2B`;
dấu cộng thô trong URL có thể bị hiểu là khoảng trắng.

## PowerShell và UTF-8

Dùng job có sẵn hoặc fixtures test, không cần worker/provider để đọc. Nạp token vào
biến môi trường của terminal theo cách riêng của bạn; `.env` không tự nạp vào PowerShell.
Lưu script tiếng Việt ở UTF-8 (Windows PowerShell 5.1 nên dùng UTF-8 BOM nếu cần).

```powershell
if (-not $env:ADMIN_TOKEN) { throw 'Nạp ADMIN_TOKEN vào terminal trước khi chạy.' }
$baseUrl = 'http://localhost:8000/api/v1/admin'
$headers = @{ Authorization = "Bearer $env:ADMIN_TOKEN" }
$metrics = Invoke-RestMethod "$baseUrl/metrics" -Headers $headers
$name = [uri]::EscapeDataString('Công ty Sen') # UTF-8 percent encoding
$page = Invoke-RestMethod "$baseUrl/jobs?company_name=$name&limit=50" -Headers $headers
$page | Select-Object total, offset, limit
$page.items | Format-Table job_id, company_name, status
if ($page.items.Count -gt 0) {
    $jobId = $page.items[0].job_id
    $detail = Invoke-RestMethod "$baseUrl/jobs/$jobId" -Headers $headers
    $events = Invoke-RestMethod "$baseUrl/jobs/$jobId/events?limit=100" -Headers $headers
    $attempts = Invoke-RestMethod "$baseUrl/jobs/$jobId/attempts?limit=100" -Headers $headers
    $detail.steps | Format-Table step_name, status, attempt_count
    $events.items | Format-Table created_at, event_type
    $attempts.items | Format-Table step_name, status, tokens_input, tokens_output
}
```

GET không có body nên không cần JSON encoding. Khi dùng POST research riêng, làm theo
[ví dụ PowerShell UTF-8](../../README.md#demo-payloads-for-the-team), gửi UTF-8 bytes.
Không đưa credential vào nội dung nghiên cứu: input/output detail giữ dữ liệu công
ty đã lưu, không thể bảo đảm raw text người dùng luôn không có secret. Không log
headers/input/output khi debug hoặc chia sẻ response chứa dữ liệu công ty.

## Đọc lỗi và quyết định của operator

NEEDS_REVIEW/UNKNOWN yêu cầu đọc error/events/attempts; API không đưa eligibility tự
suy ra. Missing evidence ở result phản ánh giới hạn nguồn. Nếu operator quyết định
retry, dùng endpoint hiện có
`POST /api/v1/research/jobs/{job_id}/retry`, xem
[hướng dẫn retry và recovery](../end-to-end.vi.md). Cần Bearer và acknowledgement
khả năng tính phí trùng; chỉ next unfinished UNKNOWN/NEEDS_REVIEW, dưới ceiling và
downstream chưa chạy. Config/prompt mismatch thường cần job mới. Tutorial đọc này
không tự gọi retry. Không có cancel, sửa/xóa job hay thay cấu hình qua admin.

[test_admin_api.py](../../tests/integration/test_admin_api.py) kiểm tra qua ASGI với
lifespan và DB thật. API tests + public API regression + architecture đạt **25 passed**
trên SQLite/PostgreSQL sau phase này; auth sai không đọc port, query sai không gọi
reader method, OpenAPI có Bearer và mọi GET bất biến. Xem phase E2E tiếp theo để
lần theo từng mũi tên HTTP→DB→JSON.
