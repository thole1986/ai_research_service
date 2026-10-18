# 01 — Domain: ý nghĩa của job và các bước

Layer này mô tả việc nghiên cứu công ty và các quyết định đúng/sai của workflow,
không xử lý HTTP hay truy vấn database. F003 chỉ thêm cách đọc dữ liệu, nên **không
cần sửa domain hoặc tạo `domain/admin`**.

[entities.py](../../src/company_ai/domain/research/entities.py) định nghĩa
`ResearchJob`, `ResearchStep`, `JobEvent`: snapshot độc lập ORM. `ResearchJob` chứa
input/result, trạng thái và policy `retry_candidate` của aggregate. Checkpoint từng
bước nằm ở `ResearchStep.output_json`, được trả như các snapshot riêng cùng job.
DTO admin chỉ chọn các trường cần xem; không gửi nguyên aggregate ra HTTP vì trong
snapshot còn có idempotency key, hash và lease token.

Ví dụ xuyên suốt: job giả định `11111111-1111-4111-8111-111111111111`, công ty
`Công ty Sen CRM`, trạng thái `NEEDS_REVIEW`. DB có RESEARCH hoàn thành, CLASSIFY
`UNKNOWN`, SCORE/REPORT `PENDING`. JSON admin sẽ hiển thị những trạng thái này và
output đã lưu, nhưng không trả token dùng để worker hoàn thành lease.

[contracts.py](../../src/company_ai/domain/research/contracts.py) có `ALL_STEPS`,
`step_order`, manual-v1 và tavily-v1. COLLECT chỉ xuất hiện ở job thu thập. Không
thêm status để phục vụ bộ lọc: job dùng PENDING/RUNNING/COMPLETED/NEEDS_REVIEW/FAILED;
attempt dùng CLAIMED/REQUEST_STARTED/SUCCEEDED/FAILED/UNKNOWN.

Caller retry hiện tại đi qua `JobService.retry` → repository →
`ResearchJob.retry_candidate(steps, step_name, max_attempts)` → step ID hoặc
`Conflict("RETRY_NOT_ALLOWED")`. Chỉ bước chưa hoàn thành đầu tiên có trạng thái
UNKNOWN/NEEDS_REVIEW, dưới trần attempt và không có downstream đã chạy mới được xét.
COLLECT còn chịu trần riêng trong repository. Admin đọc không gọi chuỗi này.

[policies.py](../../src/company_ai/domain/research/policies.py) giữ budget, kết quả
lease hết hạn và kiểm tra evidence. `UNKNOWN` nói rằng kết quả remote không chắc
chắn, không chứng minh request miễn phí hoặc chưa chạy. Missing evidence trong
result là giới hạn dữ liệu; không tự đồng nghĩa lỗi hay đủ điều kiện retry.

Chiều import hướng vào domain: application/infrastructure có thể dùng các khái
niệm trên; domain không import FastAPI, SQLAlchemy hay provider SDK. `await` ở layer
ngoài chỉ nhường event loop khi chờ I/O, không tạo Celery worker. Port cũng không
phải entity hay implementation. Xem `test_dependencies_point_toward_domain` trong
[test_architecture.py](../../tests/unit/test_architecture.py) và các test recovery
trong [test_recovery.py](../../tests/integration/test_recovery.py) khi debug policy.
