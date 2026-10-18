# 02 — Application: các thao tác xem dữ liệu

Layer này diễn đạt người vận hành muốn xem gì, độc lập cách HTTP nhận query và
database lưu dữ liệu. [admin_service.py](../../src/company_ai/application/research/admin_service.py)
có `ResearchAdminService`, chỉ nhận `ResearchAdminReader`. Nó không có publisher,
không gọi `JobService.create/retry`, không có provider adapter.

[admin_queries.py](../../src/company_ai/application/research/admin_queries.py) chứa
frozen dataclass `JobFilters`, `EventFilters`, `AttemptFilters`, `Paging`, `Page`
và các DTO `JobSummary`, `JobDetail`, `AdminStep`, `AdminEvent`, `AdminAttempt`.
`AdminMetrics` là TypedDict để giữ nguyên contract metrics F001/F002. Các kiểu này
không import FastAPI, HTTP schema hay SQLAlchemy.

Caller router → `service.list_jobs(JobFilters(status="NEEDS_REVIEW"), Paging(0, 50))`
→ `reader.list_jobs(filters, page)` → `Page[JobSummary]`. DTO trả về cho công ty Sen
chỉ có tên, website, trạng thái, thời điểm, cost và error code; danh sách không mang
input hay result. `JobFilters` trim tên và chuyển ngày có timezone về UTC, từ chối
ngày không timezone/khoảng đảo ngược. Service chặn page jobs trên 100; history tối đa 500.

Các lời gọi còn lại:

| Service → port (cùng tên method) | Input | Output |
|---|---|---|
| `get_job_detail` | job ID | `JobDetail` |
| `list_job_events` | job ID, `EventFilters`, `Paging` | `Page[AdminEvent]` |
| `list_job_attempts` | job ID, `AttemptFilters`, `Paging` | `Page[AdminAttempt]` |
| `get_metrics` | không có | `AdminMetrics` |

[admin_ports.py](../../src/company_ai/application/research/admin_ports.py) là
Protocol, chỉ khai báo hợp đồng. Adapter trả `None` khi detail/history không có job;
service đổi thành `JobNotFound` có sẵn ở [jobs.py](../../src/company_ai/application/research/jobs.py).
Job tồn tại nhưng chưa có attempts trả `Page(items=(), total=0, ...)`, không phải lỗi.
DTO input/result là bản sao dữ liệu lưu trong DB; schema API mới quyết định biểu
diễn JSON như `items: []`, datetime có timezone và số USD.

Với job Sen, `get_job_detail(id)` trả input và CLASSIFY UNKNOWN để người vận hành
đọc. Nó không tính `can_retry`. Policy vẫn nằm ở domain/repository hiện tại. NULL
tokens/cost nghĩa là chưa biết; số 0 là giá trị đã lưu bằng 0, không tự thay thế nhau.

`await reader.list_jobs(...)` chờ adapter đọc DB, nhường event loop trong I/O; không
enqueue task. Tạo service hoặc resolve Depends không chạy SQL. ORM chưa xuất hiện
ở layer này; frozen DTO ngăn gán lại field, còn JSON được adapter deepcopy trước
khi trả để không giữ tham chiếu tới dữ liệu ORM.

Debug theo service → port, không tìm SQL trong service. Unit tests trong
[test_admin_service.py](../../tests/unit/test_admin_service.py) kiểm tra delegation,
not-found, filter/page; chạy cùng architecture test đã đạt **16 passed**. Mock reader
chỉ có các read methods là đủ; kiểm thử HTTP→DB thật sẽ nằm ở phase integration.
