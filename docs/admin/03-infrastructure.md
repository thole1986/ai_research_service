# Infrastructure: đọc DB và tạo dữ liệu trả về an toàn

Layer này biến yêu cầu tìm kiếm của application thành SQL, rồi trả về dữ liệu Python
đã tách khỏi session. Nó biết cấu trúc bảng nhưng không biết HTTP, Bearer token,
publisher hay provider. Việc đọc admin không khởi chạy worker hoặc gọi LLM.

Các file thực tế:

- [admin_reader.py](../../src/company_ai/infrastructure/research/persistence/admin_reader.py):
  `SqlAlchemyAdminReader`, implementation của port `ResearchAdminReader`.
- [admin_mapping.py](../../src/company_ai/infrastructure/research/persistence/admin_mapping.py):
  chỉ lấy metadata được phép, lọc event details và collection usage, chuẩn hóa error.
- [metrics.py](../../src/company_ai/infrastructure/research/persistence/metrics.py):
  `read_metrics`, SQL metrics dùng chung với repository hiện có.
- [models.py](../../src/company_ai/infrastructure/research/persistence/models.py):
  `Job`, `Step`, `Attempt`, `Event` là ORM tương ứng các bảng research.
- [transactions.py](../../src/company_ai/infrastructure/database/transactions.py):
  `SessionOperations.session` kết thúc transaction đọc bằng rollback.
- [session.py](../../src/company_ai/infrastructure/database/session.py):
  `Database` quản lý engine/session factory và hàm Unicode cho SQLite.

## Ai gọi ai?

`ResearchAdminService` gọi các method của port với filter/page thuần Python. Bootstrap
sẽ cấp `SqlAlchemyAdminReader(session)` vào port đó. Constructor chỉ giữ session và
đọc tên backend; SQL bắt đầu khi method được `await`.

Ví dụ rút gọn đúng cách gọi adapter trong test:

```python
async with repo.db.session() as session:
    reader = SqlAlchemyAdminReader(session)
    page = await reader.list_jobs(
        JobFilters(status="NEEDS_REVIEW", company_name="sen"),
        Paging(offset=0, limit=20),
    )
```

Đầu vào là `JobFilters` và `Paging`; đầu ra là `Page[JobSummary]` chứa `items`, `total`,
`offset`, `limit`. `await` nhường vòng lặp trong lúc chờ DB, không tạo một worker mới.
Các object trả ra là dataclass application, không phải `Job` ORM hay lazy relationship.
API ở layer sau mới đổi chúng thành JSON response.

## Query, count và phân trang

Giả sử DB có job `11111111-1111-4111-8111-111111111111` của `Công ty Sen CRM`,
đang `NEEDS_REVIEW`, như ví dụ ở domain/application. `list_jobs`
dùng JSON scalar extraction cho `input_json.company_name` và `input_json.website`;
không lấy cả input, result, execution config, step hay output trong danh sách.

Một lần list chạy hai SELECT: count với bộ điều kiện, rồi projection cùng điều kiện,
`ORDER BY created_at DESC, id DESC`, `OFFSET`, `LIMIT`. Tổng số vẫn đúng nếu offset
vượt cuối và `items=()`. ID là tiêu chí phụ để thứ tự không mơ hồ khi timestamp trùng.
Thêm nhiều job không tạo thêm một query trên từng job (N+1).

`company_name` đã trim và validate trong application. Adapter escape `\`, `%`, `_`
trước khi đưa vào bind parameter của `LIKE ... ESCAPE '\'`. Vì vậy `%_\` trong tên
được tìm như ký tự thường; không trở thành wildcard hoặc chuỗi SQL.

SQLite mặc định chỉ xử lý hoa/thường ASCII trong `lower()`/`LIKE`. Mỗi connection do
`Database` tạo được đăng ký hàm `unicode_lower` dùng `str.lower()`, rồi adapter dùng
hàm đó với giá trị JSON. PostgreSQL dùng `lower()` theo locale DB. Tests với tên
tiếng Việt chạy trên cả hai backend. Đây là tìm kiếm substring có phân biệt dấu,
không phải tìm kiếm bỏ dấu; một số Unicode đặc biệt còn phụ thuộc locale PostgreSQL.

Khoảng `created_from`/`created_to` bao gồm hai đầu. Application yêu cầu timezone và
chuẩn hóa UTC; `UTCDateTime` lo chuyển kiểu khi lưu/đọc SQLite và PostgreSQL.

Không thêm index hoặc migration trong F003. Bộ lọc substring `%...%` không hưởng lợi
trực tiếp từ B-tree tên công ty thông thường; index status/job_id hiện có vẫn giữ.
Chưa có bằng chứng workload cần composite index riêng. Khi có dữ liệu thật đủ lớn,
đo EXPLAIN/query time trước khi chọn index và Alembic revision phù hợp backend.

## Detail, lịch sử và mapping

`get_job_detail(job_id)` chạy tối đa hai query: projection job rồi projection tất cả
step của job theo thứ tự `COLLECT → RESEARCH → CLASSIFY → SCORE → REPORT`. Job không
tồn tại trả `None` ngay sau query đầu. Input/result/output được deep copy trước khi
trả ra ngoài session.

Ở một job tavily-v1 khác của công ty Sen, COLLECT đã hoàn tất và RESEARCH đang dừng.
DB có `result_json=None`,
`COLLECT.output_json={"sources": [...]}`. Detail có partial result
`{"collect": {"sources": [...]}}`, giống hành vi JobView của F001; không ghi partial
result ngược vào DB. Với workflow manual chưa có final result, `result` vẫn `None`.

Metadata chỉ lấy từ `ExecutionConfig` đã lưu và validate được: provider, model,
profile_id, pricing_kind cho mỗi LLM step. Config cũ không có hoặc hỏng trả `None`;
không lấy settings hôm nay để gán cho job cũ. Không trả endpoint, connection_ref,
fingerprint, request/input/output hash, idempotency key hoặc lease token. Các mốc
`lease_expires_at`, `last_heartbeat_at`, `next_retry_at` là dữ liệu đọc tiến trình.

Error code thuộc tập category thực tế được giữ lại. Code lạ trong lịch sử trở thành
`UNKNOWN_ERROR`. Thông báo detail luôn là câu cố định
`Research job stopped; inspect sanitized events and attempts.`; không trả raw
`error_message`, stack trace hoặc provider body từ DB. Đây chỉ là cách trình bày
an toàn, không sao chép policy retry/lease/budget của domain.

`list_job_events` và `list_job_attempts` mỗi lần chạy tối đa ba SELECT: xác nhận job,
count với filter, rồi trang dữ liệu. Events lọc exact `event_type`; attempts join
`Step` để lọc `job_id`, `step_name`, `status`. Cả hai xếp `created_at ASC, id ASC`.
Job có thật nhưng không có history trả trang rỗng; job thiếu trả `None`, service
chuyển thành `JobNotFound`. Event retention có thể đã xóa một phần lịch sử.

Event details dùng allowlist theo chính event được emit trong repository:

| Nhóm event | Dữ liệu được giữ nếu đúng kiểu |
|---|---|
| STEP_CLAIMED | attempt_id UUID, step_name hợp lệ |
| LLM_REQUEST_STARTED, LLM_USAGE_RECORDED | attempt_id, metadata an toàn; usage_known/cost_reconciled là bool |
| COLLECTION_REQUEST_STARTED | attempt_id |
| MODEL_IDENTITY_PINNED | attempt_id, identity_verified bool |
| STEP_CHECKPOINTED | attempt_id, step_name, metadata an toàn |
| STEP_UNKNOWN, STEP_NEEDS_REVIEW, STEP_FAILED | attempt_id, error_category đã chuẩn hóa, metadata an toàn |
| JOB_NEEDS_REVIEW | error_category đã chuẩn hóa |
| MANUAL_RETRY_ACKNOWLEDGED | acknowledge_possible_duplicate_charge bool |
| Event khác | details rỗng |

Metadata event chỉ gồm provider, model, profile_id, pricing_kind, adapter_version.
Field tên đúng nhưng chứa object/list, boolean giả bằng số, chuỗi quá dài hoặc control
character sẽ bị loại. Event lạ và field ngoài allowlist không được echo lại.

Attempt không có lease token, config fingerprint hoặc raw response. Request ID chỉ
giữ identifier giới hạn ký tự; URL/provider content bất thường bị bỏ. `execution_type`
được xác định theo bước: COLLECT là `collection`, SCORE là `deterministic`, các bước
LLM là `llm`; đây là loại công việc kể cả khi attempt chưa gọi provider.

F001 usage chỉ nhận số hữu hạn không âm tại `usage.credits` và `credits`; bool, nested
objects và field lạ bị loại. `None` nghĩa là chưa biết/chưa lưu, số `0` là đã biết bằng
không. Không gán provider/model hoặc số token giả cho attempt cũ thiếu metadata.

Input và output nghiên cứu chỉ xuất hiện ở detail, giữ nội dung công ty đã lưu.
Chúng không phải nội dung đã lọc mọi credential do người dùng vô tình đưa vào; không
đặt secret trong dữ liệu nghiên cứu và không log input/output khi debug.

## Metrics và transaction ngắn

`get_metrics()` và `SqlAlchemyResearchRepository.metrics()` cùng gọi `read_metrics`.
Không có hai bản SQL aggregation. Helper giữ các keys/count/tokens/cost và breakdown
LLM/collection hiện tại; collection usage được kiểm tra số trước khi cộng. Không tính
lại token/cost từ output và không gọi provider. Giá USD là ước lượng đã lưu, collection
credits là đơn vị riêng; `None` khác `0`.

Adapter nhận `AsyncSession` từ request, bọc bằng `SessionOperations`. Mỗi method dùng
scope đọc ngắn và rollback trong `finally`, kể cả khi SQL lỗi; không commit, flush,
FOR UPDATE hoặc thay đổi timestamp/status/lease. Session cần idle trước mỗi method,
không dùng đồng thời hoặc lồng trong transaction khác. Scope kết thúc transaction,
còn dependency bên ngoài đóng session.

Count và trang chạy cùng session/operation nhưng không cam kết cùng snapshot tuyệt
đối khi worker đang ghi trên mọi isolation level. Offset pagination cũng có thể dịch
trang khi job mới xuất hiện. Chỉ nên hiểu `total` là count phục vụ lần đọc hiện tại.

## Kiểm tra và debug

[test_admin_reader.py](../../tests/integration/test_admin_reader.py) dùng repo fixture,
DB thật và AsyncSession thật để kiểm tra projection nhẹ, số query hữu hạn, tie order,
Unicode/literal wildcard, UTC hai đầu, trang ngoài phạm vi, missing job, history rỗng,
safe mapping, null/zero, config cũ/hỏng và DTO dùng được sau khi đóng session. Test còn
so toàn bộ row trước/sau mọi method để bảo đảm chỉ đọc, kể cả khi job có lease sống.

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_admin_reader.py -q
```

Fixture chạy SQLite và PostgreSQL; PostgreSQL cần `TEST_POSTGRES_URL` trỏ DB test.
Mỗi test dùng schema ngẫu nhiên riêng và chỉ dọn schema đó. Khi thiếu biến này,
PostgreSQL được báo skipped, không được hiểu là đã pass backend đó. Chạy thêm
`test_collection_repository.py` và `test_execution_config_repository.py` khi sửa helper
metrics để giữ tương thích F001/F002.

Khi lỗi `Research operation requires an idle session`, kiểm tra caller đang giữ
transaction cũ hoặc lồng operation. Khi SQLite báo thiếu `unicode_lower`, kiểm tra
session có dùng `Database` của dự án hay tự tạo engine bỏ qua hook connection.
Khi detail metadata là `None`, kiểm tra cấu trúc execution config đã lưu mà không log
toàn bộ nội dung cấu hình/input. Không sửa job hay gọi retry chỉ để kiểm tra đọc admin.
