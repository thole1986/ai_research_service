# Dịch vụ nghiên cứu doanh nghiệp và đánh giá khách hàng tiềm năng bằng AI

Giải thích chi tiết từ API đến báo cáo, các service tích hợp và cách xử lý lỗi: [Luồng end to end](end-to-end.vi.md).

Muốn chạy riêng từng service: [Các file Compose và lệnh chạy tương ứng](compose-services.vi.md). File tổng hợp vẫn dùng được như trước.

Tài liệu tiếng Việt tương ứng với [README chính](../README.md). Các lệnh bên dưới chạy từ thư mục gốc dự án. Phần cập nhật DDD, async, cấu hình PostgreSQL và pool nằm tại [kiến trúc và database](architecture.vi.md).

Hệ thống nhận thông tin doanh nghiệp và chân dung khách hàng lý tưởng (ICP), xử lý bất đồng bộ rồi trả về báo cáo có dẫn chứng từ văn bản được cung cấp.

Luồng xử lý: **FastAPI → Redis/Celery → CrewAI/OpenAI**. SQLite local hoặc PostgreSQL lưu trạng thái nghiệp vụ, kết quả từng bước và lịch sử thực thi, truy cập qua SQLAlchemy async. Mã nguồn tách `domain`, `application`, `infrastructure` và API; chi tiết tại [tài liệu DDD](architecture.vi.md).

## 1. Các bước xử lý

| Bước | Chức năng | Cách thực hiện |
|---|---|---|
| `RESEARCH` | Trích xuất ngành nghề, sản phẩm, nhóm khách hàng, dẫn chứng và hạn chế dữ liệu | CrewAI + OpenAI |
| `CLASSIFY` | Đánh giá mức độ phù hợp với ICP, chỉ ra tín hiệu phù hợp và thông tin còn thiếu | CrewAI + OpenAI |
| `SCORE` | Tính điểm và xếp hạng theo quy tắc công khai | Python, không gọi LLM |
| `REPORT` | Tổng hợp báo cáo, điểm mạnh, rủi ro và câu hỏi cần xác minh | CrewAI + OpenAI |

Mỗi bước lưu checkpoint riêng. Worker phải giành quyền xử lý bằng thao tác nguyên tử trong DB; lease giới hạn thời gian sở hữu, còn fencing token ngăn worker cũ ghi đè kết quả.

Nguồn thông tin chỉ gồm mô tả doanh nghiệp và nội dung website do người dùng cung cấp. Địa chỉ website được lưu làm thông tin tham chiếu; hệ thống không tự truy cập hay thu thập website.

## 2. Chạy bằng Docker với Redis có sẵn

Yêu cầu: Docker và Docker Compose hoạt động; Redis hiện có được công bố qua cổng `6379` trên máy host.

```powershell
# Chỉ sao chép khi chưa có .env để tránh ghi đè cấu hình hiện tại.
Copy-Item .env.example .env
# Tự điền OPENAI_API_KEY và ADMIN_TOKEN ngẫu nhiên trong .env.
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8000/ready
```

Swagger: **http://localhost:8000/docs**.

Compose mặc định sử dụng Redis bên ngoài qua:

```dotenv
CONTAINER_REDIS_URL=redis://host.docker.internal:6379/0
```

Nếu Redis dùng cổng, mật khẩu hoặc host khác, sửa biến này cho phù hợp. Chạy Python trực tiếp trên máy sẽ dùng `REDIS_URL=redis://localhost:6379/0`.

**Mặc định không khởi động thêm container Redis.**

Sau khi thêm hoặc sửa `.env`, tạo lại container để các tiến trình nhận cấu hình mới:

```powershell
docker compose up -d --force-recreate
```

### Các service trong Compose

| Service | Vai trò | Script khởi động |
|---|---|---|
| `migrate` | Chạy Alembic một lần trước khi các service ứng dụng khởi động | `scripts/start-migrate.sh` |
| `api` | Cung cấp REST API và Swagger | `scripts/start-api.sh` |
| `ai-worker` | Xử lý pipeline trên queue `ai` | `scripts/start-ai-worker.sh` |
| `maintenance-worker` | Xử lý tác vụ bảo trì trên queue `maintenance` | `scripts/start-maintenance-worker.sh` |
| `beat` | Lập lịch quét lease, tiếp tục job chờ và dọn sự kiện cũ | `scripts/start-beat.sh` |
| `verification` | Chạy lint, kiểm tra định dạng và test; chỉ bật khi cần | `scripts/start-verification.sh` |
| `redis` | Redis tùy chọn dành cho máy chưa có Redis | `scripts/start-redis.sh` |

Mỗi service có Dockerfile riêng tại `docker/<tên-service>/Dockerfile` và chạy qua script `.sh`. Các service ứng dụng chia sẻ volume `app-data` để truy cập cùng một file SQLite. Với MVP này, chỉ chạy một AI worker, một maintenance worker và một Beat.

## 3. Phát triển trực tiếp trên Windows PowerShell

Sử dụng Python 3.11–3.13 và cài `uv` trước. Dự án đã được kiểm thử trên Windows với Python 3.13.13 và trong Linux container với Python 3.13.16.

```powershell
uv sync --extra dev --frozen
# Bỏ qua lệnh sao chép nếu đã có .env.
Copy-Item .env.example .env
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\uvicorn company_ai.main:app --host 127.0.0.1 --port 8000
```

Mở thêm ba terminal, kích hoạt môi trường ảo trong từng terminal bằng `.\.venv\Scripts\Activate.ps1`, rồi chạy mỗi lệnh dưới đây ở một terminal riêng:

```powershell
celery -A company_ai.celery_app:celery_app worker -Q ai --pool=solo -c 1 -l info
celery -A company_ai.celery_app:celery_app worker -Q maintenance --pool=solo -c 1 -l info
celery -A company_ai.celery_app:celery_app beat -l info --schedule data/celerybeat-schedule
```

Trên WSL/Linux, kích hoạt bằng `source .venv/bin/activate`, sau đó chạy các lệnh tương ứng hoặc `sh scripts/start-<service>.sh`.

File `uv.lock` khóa phiên bản các dependency trực tiếp và gián tiếp cho Windows/Linux. SQLite phải nằm trên filesystem cục bộ của một máy; không dùng database trên ổ mạng. Khi chạy trực tiếp trên host, giữ `DATABASE_URL` như ví dụ `.env.example`, không dùng đường dẫn `/app/data` bên trong container.

## 4. Các cấu hình chính

| Biến | Ý nghĩa |
|---|---|
| `DB_BACKEND` | Chọn `sqlite` hoặc `postgresql` khi URL để trống |
| `DATABASE_URL` | DSN explicit cho host, ưu tiên hơn `DB_BACKEND`; mặc định để trống |
| `CONTAINER_DATABASE_URL` | DSN explicit cho container; để trống để dùng backend và các biến thành phần |
| `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT` | Cấu hình PostgreSQL dùng driver `asyncpg` |
| `DB_POOL_MODE` | `pooled` hoặc `null`; xem cấu hình pool trong tài liệu kiến trúc |
| `REDIS_URL` | Địa chỉ Redis khi chạy trực tiếp trên máy |
| `CONTAINER_REDIS_URL` | Địa chỉ Redis mà các container ứng dụng sử dụng |
| `OPENAI_API_KEY` | API key; chỉ lưu cục bộ trong `.env` |
| `OPENAI_MODEL` | Model sử dụng cho các bước AI |
| `ADMIN_TOKEN` | Token bảo vệ endpoint retry và metrics; để trống thì không thể truy cập các endpoint này |
| `MAX_INPUT_CHARS` | Giới hạn tổng số ký tự đầu vào |
| `MAX_OUTPUT_TOKENS_PER_STEP` | Giới hạn token đầu ra cho mỗi bước AI |
| `MAX_LLM_CALLS_PER_JOB` | Số lần bắt đầu gọi LLM tối đa cho một job, tính cả retry thủ công |
| `MAX_STEP_ATTEMPTS` | Số lần thực thi tối đa của mỗi bước |
| `MAX_ESTIMATED_USD_PER_JOB` | Ngân sách ước tính cho một job |
| `INPUT_USD_PER_MILLION`, `OUTPUT_USD_PER_MILLION` | Đơn giá dùng để ước tính chi phí; cần cập nhật khi thay model hoặc giá |
| `LLM_REQUEST_TIMEOUT_SECONDS` | Timeout của request tới nhà cung cấp |
| `LEASE_TTL_SECONDS` | Thời hạn quyền sở hữu bước; phải lớn hơn timeout theo điều kiện kiểm tra cấu hình |
| `RECOVERY_SCAN_SECONDS` | Chu kỳ quét phục hồi, mặc định 60 giây |

Không đưa `.env`, API key hoặc database vào mã nguồn hay chia sẻ chúng trong chat.

## 5. Ví dụ gọi API

### Tạo job và xem kết quả

```powershell
$headers = @{ 'Idempotency-Key' = 'demo-acme-v1' }
$body = @{
  company_name = 'Acme CRM'
  website = 'https://example.com'
  company_description = 'Acme provides B2B SaaS CRM software for SMEs in Vietnam.'
  website_text = ''
  icp = 'B2B SaaS for SMEs in Vietnam'
} | ConvertTo-Json

$job = Invoke-RestMethod http://localhost:8000/api/v1/research/jobs -Method Post -Headers $headers -ContentType application/json -Body $body
Invoke-RestMethod "http://localhost:8000/api/v1/research/jobs/$($job.job_id)"
Invoke-RestMethod "http://localhost:8000/api/v1/research/jobs/$($job.job_id)/events"
```

Mỗi yêu cầu nghiệp vụ mới cần một `Idempotency-Key` riêng. Gửi lại cùng key và cùng nội dung đã chuẩn hóa sẽ trả về job cũ.

| Mã HTTP | Ý nghĩa |
|---|---|
| `202` | Đã tạo job hoặc đã chấp nhận retry thủ công |
| `200` | Đọc dữ liệu thành công hoặc yêu cầu tạo trùng trả về job cũ |
| `401` | Thiếu hoặc sai token quản trị |
| `404` | Không tìm thấy job |
| `409` | Cùng key nhưng khác nội dung, hoặc bước không đủ điều kiện retry |
| `413` | Kích thước request vượt giới hạn |
| `422` | Dữ liệu hoặc độ dài đầu vào không hợp lệ |
| `503` | Endpoint readiness không kết nối được dependency cần thiết |

Kết quả truy vấn job chứa trạng thái, các bước theo thứ tự, số lần thực thi, thời gian UTC, lỗi đã được làm sạch và chi phí ước tính. Khi hoàn tất, trường `result` gồm `research`, `classify`, `score`, `report`.

### Retry thủ công

Gọi `POST /api/v1/research/jobs/{job_id}/retry` với header:

```text
Authorization: Bearer <ADMIN_TOKEN>
```

Nội dung JSON:

```json
{
  "step_name": "RESEARCH",
  "acknowledge_possible_duplicate_charge": true
}
```

Chỉ được retry bước chưa hoàn thành đầu tiên đang ở `UNKNOWN` hoặc `NEEDS_REVIEW`, khi các bước phía sau vẫn ở `PENDING` và chưa vượt giới hạn số lần thực thi. Không retry bước đã hoàn thành hoặc đang chạy.

Việc xác nhận đồng nghĩa bạn chấp nhận khả năng phát sinh phí trùng cho request cũ có kết quả không xác định. Retry vẫn chịu giới hạn số lần gọi và ngân sách. Nếu thay các giới hạn này, cần khởi động lại service; nếu checkpoint không còn phù hợp với prompt/cấu hình hiện tại, tạo job mới với key mới.

### Endpoint vận hành

| Endpoint | Mục đích |
|---|---|
| `GET /health` | Kiểm tra tiến trình API còn hoạt động |
| `GET /ready` | Kiểm tra database đã migration và kết nối Redis; không gọi OpenAI |
| `GET /api/v1/research/jobs/{job_id}/events` | Đọc lịch sử sự kiện của job |
| `GET /api/v1/admin/metrics` | Xem số lượng job/bước/sự kiện, lần gọi LLM, token và chi phí; yêu cầu token quản trị |

## 6. Checkpoint, recovery và chi phí

- Claim bước và tạo bản ghi attempt nằm trong cùng một transaction database. Hoàn tất bước cần đúng lease token, input hash, trạng thái `RUNNING` và lease chưa hết hạn. Kết quả đã hoàn thành không bị ghi đè.
- Không giữ transaction DB trong lúc gọi nhà cung cấp. Heartbeat gia hạn lease khi xử lý lâu; worker đã mất lease không thể tự khôi phục quyền ghi. Hash bao gồm dữ liệu liên quan, schema, prompt, model và cấu hình.
- Timeout hoặc crash khiến kết quả gọi LLM không rõ sẽ chuyển sang `UNKNOWN`/`NEEDS_REVIEW`. Hệ thống không tự gọi lại LLM trong trường hợp này. `REQUEST_STARTED` chỉ ghi nhận ý định gọi, không chứng minh request đã được xử lý hay tính phí.
- Request bị từ chối rõ ràng với HTTP `429` được thử lại có giới hạn và tăng thời gian chờ theo cấp số nhân qua trạng thái DB. Retry của OpenAI SDK bị tắt; mỗi Crew có cổng giới hạn một SDK request để tránh các lần sửa định dạng hoặc lặp nội bộ phát sinh thêm lời gọi trả phí.
- Trước khi gọi, hệ thống giữ lại một khoản chi phí ước tính từ prompt/schema, phần dự phòng và giới hạn đầu ra. Khi thành công, điều chỉnh theo usage; khi kết quả không rõ, vẫn giữ khoản ước tính. Đây là kiểm soát theo ước tính, không phải sổ thanh toán hay giới hạn USD cứng do nhà cung cấp bảo đảm.
- Dẫn chứng nghiên cứu phải xuất hiện trong văn bản đầu vào. Tín hiệu phân loại phải trích từ dẫn chứng đó. Kiểm tra này xác nhận nguồn trích dẫn, không xác nhận thông tin doanh nghiệp là sự thật.
- Beat mặc định lập lịch quét lease hết hạn và tiếp tục job chờ mỗi 60 giây; dọn sự kiện cũ lúc **02:00 UTC, tức 09:00 giờ Việt Nam**. Cập nhật có điều kiện trong DB bảo vệ khi các lượt quét chạy chồng nhau.
- Quét job chờ giúp khôi phục khi đã lưu DB nhưng chưa gửi được message tới Redis, kể cả crash sau checkpoint. Message trùng vẫn phải qua cổng claim trong DB.
- Log chỉ ghi định danh, loại sự kiện/lỗi và số liệu sử dụng; không ghi toàn bộ đầu vào, prompt, response hay lỗi thô từ nhà cung cấp. Nội dung kết quả được lưu trong checkpoint DB và trả qua API. Telemetry CrewAI bị tắt.

### Quy tắc chấm điểm

| Tiêu chí | Điểm tối đa |
|---|---:|
| Dẫn chứng có tín hiệu B2B và SaaS | 40 |
| Phân loại phù hợp với ICP, có tín hiệu dẫn chứng hợp lệ | 30 |
| Có dẫn chứng phục vụ SME/doanh nghiệp nhỏ và vừa | 20 |
| Có sản phẩm xuất hiện trong dẫn chứng | 10 |

Tổng điểm từ 0–100: `HIGH` khi ≥70, `MEDIUM` khi ≥40 và dưới 70, còn lại là `LOW`. Đây là ước tính theo quy tắc từ văn bản được cung cấp, không phải chất lượng doanh nghiệp đã được xác minh bên ngoài.

## 7. Kiểm thử

### Kiểm thử tự động trên máy

```powershell
.\.venv\Scripts\pytest -q --basetemp=data/pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
.\.venv\Scripts\alembic check
```

Test tự động không cần OpenAI API key và giả lập ranh giới nhà cung cấp. Có test sử dụng CrewAI thật với SDK được giả lập. Test Redis thật là tùy chọn; xem lệnh container bên dưới để chạy cùng bộ kiểm thử.

### Kiểm thử trong Linux container với Redis có sẵn

```powershell
docker compose --profile test run --rm --no-deps --build -e RUN_REDIS_E2E=1 -e TEST_REDIS_URL=redis://host.docker.internal:6379/15 verification
```

Test này dùng queue riêng để kiểm tra luồng API → Redis → Celery worker → checkpoint SQLite; LLM vẫn được giả lập. Sửa `TEST_REDIS_URL` nếu Redis của bạn dùng cấu hình khác.

Kết quả sau bản cập nhật async/DDD: **53 test pass trên Windows và 53 test pass trong Linux container**, bao gồm SQLite, PostgreSQL thật và Redis thật. Lint/format đạt; Alembic không phát hiện lệch schema. CrewAI có cảnh báo deprecated từ thư viện. Chi tiết ma trận lỗi và bằng chứng nằm tại [verification.md](verification.md).

### Smoke test OpenAI thật — có phát sinh chi phí

Sau khi cấu hình key, model, ngân sách và khởi động các service:

```powershell
.\.venv\Scripts\python scripts/run_demo.py --real-provider --acknowledge-cost
```

Lệnh này cần môi trường Python đã cài dependency bằng `uv sync --extra dev --frozen`, kể cả khi các service chạy bằng Docker. Script tạo một job ngắn, chờ hoàn tất và kiểm tra gửi trùng không làm tăng số lần thực thi.

**Khi bàn giao ban đầu, smoke test OpenAI thật chưa chạy vì chưa có API key cục bộ.** Không coi kết quả test giả lập là bằng chứng đã gọi nhà cung cấp thật thành công.

## 8. Thử crash và khôi phục

Chỉ dùng trong môi trường `APP_ENV=development`.

1. Đặt `FAILURE_POINT=checkpoint` và `FAILURE_STEP=RESEARCH` trong `.env`.
2. Tạo lại AI worker bằng `docker compose up -d --force-recreate ai-worker`, rồi gửi job mới.
3. Quan sát worker thoát với mã 86 sau khi đã lưu checkpoint RESEARCH. Khi có chính sách tự khởi động lại, container có thể tự lên lại.
4. Xóa giá trị `FAILURE_POINT`, tạo lại AI worker và chờ Beat đưa phần việc còn lại vào queue.
5. Xác nhận job tiếp tục từ CLASSIFY; số lần thực thi RESEARCH vẫn là 1.

Để thử kết quả không xác định, dùng `FAILURE_POINT=request_started` hoặc `response`. Sau khi xóa hook và tạo lại worker, chờ lease cuối cùng hết hạn cộng thêm chu kỳ quét; bước phải thành `UNKNOWN` và job thành `NEEDS_REVIEW`, không tự gọi lại LLM.

Các hook lỗi không được phép bật ngoài môi trường development. Chi tiết nằm trong [runbook kiểm thử](verification.md).

## 9. Redis tùy chọn cho máy chưa có Redis

Bạn đang có Redis bên ngoài nên có thể bỏ qua mục này.

```powershell
docker compose --profile bundled-redis up -d redis
```

Redis tùy chọn công bố cổng host `6380` để tránh trùng Redis hiện có. Muốn các container ứng dụng dùng Redis này, đặt:

```dotenv
CONTAINER_REDIS_URL=redis://redis:6379/0
```

Sau đó tạo lại các service ứng dụng để nhận cấu hình mới. Nếu chạy Python trực tiếp trên host với Redis tùy chọn, dùng `REDIS_URL=redis://localhost:6380/0`.

## 10. Vận hành và xử lý sự cố

```powershell
docker compose ps
docker compose logs --tail=100 api ai-worker maintenance-worker beat
docker compose down
```

`docker compose down` giữ lại volume. Không xóa volume khi chỉ muốn khởi động lại hệ thống.

| Hiện tượng | Kiểm tra |
|---|---|
| `/ready` trả `503` | Host/cổng/mật khẩu Redis, khả năng kết nối từ container và log migration |
| Job ở `PENDING` lâu | AI worker, queue `ai`, maintenance worker và Beat có hoạt động không |
| Job thành `NEEDS_REVIEW` | Xem events, key, ngân sách và checkpoint; cân nhắc retry có xác nhận hoặc tạo job mới |
| SQLite báo `database is locked` | Có worker chạy trùng hoặc DB nằm trên ổ mạng không |
| Sửa `.env` nhưng hành vi chưa đổi | Tạo lại container hoặc khởi động lại các tiến trình chạy trực tiếp |

SQLite WAL chỉ có một writer tại một thời điểm; hệ thống dùng transaction ngắn, busy timeout 5 giây và retry DB có giới hạn. Khi sao lưu, dùng SQLite backup API hoặc dừng service trước khi sao chép. Chỉ sao chép file `.db` khi WAL còn hoạt động có thể mất dữ liệu.

API hiện phục vụ người vận hành đáng tin cậy trên máy cục bộ và chỉ công bố cổng host ở loopback. Trước khi triển khai cho người dùng bên ngoài, cần bổ sung xác thực, phân tách tenant, quota, HTTPS và chính sách lưu giữ dữ liệu.

## 11. Giới hạn và hướng phát triển

Hệ thống **không bảo đảm gọi nhà cung cấp hoặc tính phí đúng một lần**, đặc biệt khi đã nhận response nhưng chưa kịp lưu checkpoint.

PostgreSQL đã được hỗ trợ qua `asyncpg`, với session async, khóa hàng job và bộ test recovery đồng thời chạy trên cả hai DB. Hướng dẫn chuyển cấu hình nằm tại [kiến trúc và database](architecture.vi.md). Các bước phát triển tiếp theo gồm transactional outbox để gửi queue tin cậy hơn, sổ usage nhà cung cấp, xác thực đầy đủ và giao diện duyệt các job cần kiểm tra.

Tài liệu tham khảo của README gốc: [CrewAI tasks](https://docs.crewai.com/en/concepts/tasks), [Celery periodic tasks](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html), [OpenAI GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini). Model và đơn giá trong cấu hình có thể thay đổi; cần xác nhận quyền truy cập và giá áp dụng cho tài khoản trước khi chạy thật.
