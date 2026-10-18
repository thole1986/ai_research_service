# Dịch vụ nghiên cứu doanh nghiệp và đánh giá khách hàng tiềm năng bằng AI

F002 hỗ trợ OpenAI, Claude (Anthropic) và Ollama theo từng bước, với snapshot bất biến
cho mỗi job. Mặc định vẫn OpenAI; không cần cài Ollama. Xem [cấu hình LLM routing](llm-routing.vi.md).

Xem [Bản đồ hệ thống: sơ đồ, thành phần và luồng DDD](system-map.vi.md) để nắm tổng quan trước khi đọc code.

Giải thích chi tiết từ API đến báo cáo, các service tích hợp và cách xử lý lỗi: [Luồng end to end](end-to-end.vi.md).

Muốn chạy riêng từng service: [Các file Compose và lệnh chạy tương ứng](compose-services.vi.md). File tổng hợp vẫn dùng được như trước.

Tài liệu tiếng Việt tương ứng với [README chính](../README.md). Các lệnh bên dưới chạy từ thư mục gốc dự án. Phần cập nhật DDD, async, cấu hình PostgreSQL và pool nằm tại [kiến trúc và database](architecture.vi.md).

Hệ thống nhận thông tin doanh nghiệp và chân dung khách hàng lý tưởng (ICP), xử lý bất đồng bộ rồi trả về báo cáo có dẫn chứng từ văn bản operator hoặc nguồn web được lưu qua Tavily.

Luồng xử lý: **FastAPI → Redis/Celery → CrewAI/OpenAI**. SQLite local hoặc PostgreSQL lưu trạng thái nghiệp vụ, kết quả từng bước và lịch sử thực thi, truy cập qua SQLAlchemy async. Mã nguồn tách `domain`, `application`, `infrastructure` và API; chi tiết tại [tài liệu DDD](architecture.vi.md).

## 1. Các bước xử lý

| Bước | Chức năng | Cách thực hiện |
|---|---|---|
| `COLLECT` (chỉ Tavily mode) | Tìm và lưu nguồn web trước Research, tối đa một Search request mỗi attempt | Tavily REST API qua httpx async, không gọi LLM |
| `RESEARCH` | Trích xuất ngành nghề, sản phẩm, nhóm khách hàng, dẫn chứng và hạn chế dữ liệu | CrewAI + OpenAI |
| `CLASSIFY` | Đánh giá mức độ phù hợp với ICP, chỉ ra tín hiệu phù hợp và thông tin còn thiếu | CrewAI + OpenAI |
| `SCORE` | Tính điểm và xếp hạng theo quy tắc công khai | Python, không gọi LLM |
| `REPORT` | Tổng hợp báo cáo, điểm mạnh, rủi ro và câu hỏi cần xác minh | CrewAI + OpenAI |

Mỗi bước lưu checkpoint riêng. Worker phải giành quyền xử lý bằng thao tác nguyên tử trong DB; lease giới hạn thời gian sở hữu, còn fencing token ngăn worker cũ ghi đè kết quả.

Mặc định `collection_mode="manual"`: giữ bốn bước và chỉ dùng mô tả/nội dung website do người dùng cung cấp. Với `collection_mode="tavily"`, job chạy năm bước, bắt đầu bằng COLLECT; chỉ cần tên và ICP, website tùy chọn, mô tả có thể bỏ trống. ICP không được đưa vào query và không là evidence. Text operator có nguồn riêng, không tự gán cho URL website. Backend chỉ lưu text Tavily trả về, không trực tiếp tải các URL kết quả. Xem [hướng dẫn Tavily](tavily-collection.vi.md).

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
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Điền OPENAI_API_KEY trong .env và kiểm tra Redis tại REDIS_URL trước khi chạy.
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
| `TAVILY_API_KEY` | Mặc định rỗng; chỉ cần khi dùng `collection_mode="tavily"`; manual không cần key này |
| `TAVILY_REQUEST_TIMEOUT_SECONDS` | Mặc định 30 giây, phải dương và nhỏ hơn lease TTL trên 10 giây |
| `TAVILY_MAX_RESULTS` | Mặc định 5, phạm vi 1–5 nguồn web |
| `COLLECT_MAX_ATTEMPTS` | Mặc định 2, bị giới hạn thêm bởi `MAX_STEP_ATTEMPTS`; bao gồm retry thủ công |
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

Không đưa `.env`, API key hoặc database vào mã nguồn hay chia sẻ chúng trong chat. Trước khi chạy code cập nhật, dùng `alembic upgrade head` để áp dụng revision `26a1018_tavily_collection`. Job cũ tiếp tục là `manual-v1`, không bị thêm COLLECT hoặc thay checkpoint.

## 5. Ví dụ gọi API

### Bộ JSON demo có sẵn sau khi clone

[README chính có bảng ví dụ và lệnh Postman/PowerShell chạy đầy đủ](../README.md#demo-payloads-for-the-team).
Các file nằm tại `examples/research/`:

- [MISA AMIS CRM](../examples/research/misa-amis-crm.json): công ty thật, nội dung tiếng Việt
  tóm tắt từ tài liệu chính thức của MISA; nguồn và ngày kiểm tra được ghi trong README chính.
- [Lotus CRM Demo](../examples/research/lotus-crm-fit.json): dữ liệu giả lập có rõ B2B/SaaS/SME.
- [Bánh Mì Demo](../examples/research/banh-mi-retail-no-fit.json): dữ liệu giả lập bán lẻ đồ ăn,
  không cùng lĩnh vực với ICP cung cấp phần mềm CRM.
- [Nova Demo](../examples/research/nova-insufficient-evidence.json): dữ liệu giả lập thiếu
  thông tin, `website: null`, `website_text` rỗng.

Trong Postman, chọn `POST /api/v1/research/jobs`, **Body → raw → JSON**, dán nguyên
nội dung file và đặt header `Content-Type: application/json`, `Idempotency-Key: demo-misa-001`.
Mỗi lượt demo mới đổi key; dùng lại key sẽ trả về job cũ. Khi dùng PowerShell với tiếng Việt,
đọc file bằng `-Encoding UTF8` và gửi bằng `System.Text.Encoding.UTF8.GetBytes` như ví dụ
README chính. Cần API, Redis, database đã migrate và các worker/Beat hoạt động.

Các payload manual không truy cập URL; `website_text` trong ví dụ MISA là bản tóm tắt do người
vận hành cung cấp, không phải trích nguyên văn. Scoring hiện nhận keyword tiếng Anh
B2B/SaaS/SME nên không nên dùng điểm số của ví dụ tiếng Việt làm khẳng định về công ty
thật. Mở `rule_breakdown`, `limitations`, `missing_signals` để giải thích kết quả;
dùng Lotus để demo riêng các quy tắc keyword. Các request demo gọi OpenAI thật khi
worker đã được cấu hình key.

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

Kết quả truy vấn job chứa trạng thái, các bước theo thứ tự, số lần thực thi, thời gian UTC, lỗi đã được làm sạch và chi phí LLM ước tính. Khi hoàn tất, trường `result` gồm `research`, `classify`, `score`, `report`; Tavily mode thêm `collect`, mapping `research.evidence_sources` và danh sách URL `report.sources`.

Để thử mode thu thập, gửi body dưới đây với key mới sau khi cấu hình `TAVILY_API_KEY`:

```json
{
  "company_name": "Example Company",
  "website": "https://example.com",
  "icp": "B2B SaaS phục vụ SME tại Việt Nam",
  "collection_mode": "tavily"
}
```

Mỗi attempt COLLECT có thể phát sinh phí Tavily riêng. Chi tiết payload demo, giới hạn nguồn và xử lý lỗi tại [thu thập nguồn Tavily](tavily-collection.vi.md).

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
Tavily mode chấp nhận thêm `step_name="COLLECT"` với cùng xác nhận phí trùng và trần collection riêng.

Việc xác nhận đồng nghĩa bạn chấp nhận khả năng phát sinh phí trùng cho request cũ có kết quả không xác định. Retry vẫn chịu giới hạn số lần gọi và ngân sách. Nếu thay các giới hạn này, cần khởi động lại service; nếu checkpoint không còn phù hợp với prompt/cấu hình hiện tại, tạo job mới với key mới.

### Endpoint vận hành

| Endpoint | Mục đích |
|---|---|
| `GET /health` | Kiểm tra tiến trình API còn hoạt động |
| `GET /ready` | Kiểm tra database đã migration và kết nối Redis; không gọi OpenAI/Tavily |
| `GET /api/v1/research/jobs/{job_id}/events` | Đọc lịch sử sự kiện của job |
| `GET /api/v1/admin/metrics` | Xem job/bước/sự kiện, lần gọi LLM, token và USD ước tính; collection calls/credits tách riêng; yêu cầu token quản trị |

## 6. Checkpoint, recovery và chi phí

- Claim bước và tạo bản ghi attempt nằm trong cùng một transaction database. Hoàn tất bước cần đúng lease token, input hash, trạng thái `RUNNING` và lease chưa hết hạn. Kết quả đã hoàn thành không bị ghi đè.
- Không giữ transaction DB trong lúc gọi nhà cung cấp. Heartbeat gia hạn lease khi xử lý lâu; worker đã mất lease không thể tự khôi phục quyền ghi. Hash bao gồm dữ liệu liên quan, schema, prompt, model và cấu hình.
- Timeout hoặc crash khiến kết quả gọi LLM không rõ sẽ chuyển sang `UNKNOWN`/`NEEDS_REVIEW`. Hệ thống không tự gọi lại LLM trong trường hợp này. `REQUEST_STARTED` chỉ ghi nhận ý định gọi, không chứng minh request đã được xử lý hay tính phí.
- Request bị từ chối rõ ràng với HTTP `429` được thử lại có giới hạn và tăng thời gian chờ theo cấp số nhân qua trạng thái DB. Retry của OpenAI SDK bị tắt; mỗi Crew có cổng giới hạn một SDK request để tránh các lần sửa định dạng hoặc lặp nội bộ phát sinh thêm lời gọi trả phí.
- Trước khi gọi, hệ thống giữ lại một khoản chi phí ước tính từ prompt/schema, phần dự phòng và giới hạn đầu ra. Khi thành công, điều chỉnh theo usage; khi kết quả không rõ, vẫn giữ khoản ước tính. Đây là kiểm soát theo ước tính, không phải sổ thanh toán hay giới hạn USD cứng do nhà cung cấp bảo đảm.
- COLLECT ghi request intent/counter riêng, không tăng LLM calls hoặc giữ chỗ USD của LLM. Timeout/network/5xx/crash chưa rõ kết quả cần duyệt; checkpoint đã hoàn thành được dùng lại khi resume. Usage/credits chỉ ghi khi provider thực sự trả về, không mặc định chi phí là 0.
- Dẫn chứng nghiên cứu phải xuất hiện trong nguồn hiệu lực. Tavily mode kiểm tra thêm `source_id` và quote nguyên văn trong đúng source; Report phải sao chép URL đã lưu và có limitations. Tín hiệu phân loại phải trích từ evidence. Kiểm tra này xác nhận nguồn trích dẫn, không xác nhận thông tin doanh nghiệp là sự thật.
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

Test tự động không cần OpenAI/Tavily API key và giả lập ranh giới nhà cung cấp. Có test sử dụng CrewAI thật với SDK được giả lập. Test Redis thật là tùy chọn; xem lệnh container bên dưới để chạy cùng bộ kiểm thử.

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

### Langfuse tùy chọn

Langfuse được tích hợp theo port `WorkflowObserver` ở application, adapter tại
`infrastructure/telemetry/langfuse.py`, và `NoOpObserver` khi không bật. Composition
root trong `runtime.py` khởi tạo observer trong process worker và inject cùng observer
vào workflow/CrewAI adapter. Application/domain không import Langfuse hay OpenTelemetry.

Nếu đã cài Langfuse trong đúng virtualenv đang chạy worker thì không cần cài lại.
Để cài qua cấu hình dự án:

```powershell
pip install -e ".[observability]"
# Hoặc, nếu dùng uv và cần cả dependencies cho test:
uv sync --extra dev --extra observability
```

Thêm các biến sau vào `.env`, dùng key và URL project của bạn:

```dotenv
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENABLED=true
LANGFUSE_TRACING_ENVIRONMENT=development
LANGFUSE_CAPTURE_CONTENT=false
LANGFUSE_TIMEOUT_SECONDS=2
OTEL_SDK_DISABLED=false
```

Restart worker sau khi sửa `.env`. `LANGFUSE_BASE_URL` cần trỏ đúng region hoặc host
self-hosted. `LANGFUSE_HOST` cũ vẫn được hỗ trợ nếu không đặt `LANGFUSE_BASE_URL`.
Langfuse SDK 5 yêu cầu server Langfuse 4; server self-hosted 3 dùng SDK 4
(`pip install "langfuse>=4,<5"`). Xem
[tài liệu tương thích SDK](https://langfuse.com/docs/observability/sdk/overview).
Extra `observability` không nằm trong dependencies mặc định hay image Docker mặc định;
nếu dùng Docker cần cài extra này trong image worker.

Có đủ hai key và package thì tracing tự bật. Thiếu key, chưa cài package, tắt
`LANGFUSE_TRACING_ENABLED`, hoặc `OTEL_SDK_DISABLED=true` thì dùng no-op. Biến
`OTEL_SDK_DISABLED` là công tắc tắt toàn bộ tracing, nên phải để `false` khi dùng
Langfuse; telemetry riêng của CrewAI vẫn tắt qua `CREWAI_TELEMETRY_DISABLED=true`.

Mỗi lượt thực thi tạo trace `company-research`, tra cứu qua metadata `job_id`.
Bên dưới là span của từng step và generation `openai.chat.completions`, `anthropic.messages`
hoặc `ollama.chat` theo route thực tế, với
`attempt_id`, model, thời gian, token, chi phí ước tính và mã lỗi đã lọc.
COLLECT có span `external_collection` riêng, không có model/generation LLM;
query, URL và text nguồn cũng chỉ được capture khi opt-in, không đưa vào metadata mặc định.
Checkpoint được dùng lại có trạng thái `CHECKPOINT_HIT`, không gọi lại LLM.
Retry/resume tạo trace thực thi mới, liên kết bằng cùng `job_id`.
Mặc định không gửi company text, prompt hoặc output; muốn xem chúng trong Langfuse,
đặt `LANGFUSE_CAPTURE_CONTENT=true`. Mã lỗi không chứa raw exception/provider response.

Khi bật ghi nội dung, root `company-research` có input là JSON request và output gồm
`job_id`, `status`, `result` và `error`. Khi lỗi, `result` chỉ chứa checkpoint đã hợp lệ.
Mỗi step có payload riêng và output riêng; `SCORE` có input research/classification
và output điểm cùng breakdown. Step dùng lại checkpoint cũng có đủ input/output.
Output bị validator từ chối được giữ trên step lỗi để debug, không được lưu thành
checkpoint. Model parameters, prompt/schema/rules version, input hash, job/session
và attempt metadata giúp đối chiếu từng lần chạy và được truyền xuống generation.

Nếu không thấy các cột input/output, trước hết kiểm tra `LANGFUSE_CAPTURE_CONTENT`
trong môi trường của worker và restart worker. Với SDK 5/Langfuse 4, nội dung tổng
nằm ở root observation; xem **Observations** hoặc bấm root/step trong cây trace.
Cột trace input/output cũ có thể vẫn trống dù observation đã có đủ dữ liệu. Xem
[hướng dẫn chính thức về input/output trống](https://langfuse.com/faq/all/empty-trace-input-and-output).
Một trace có một root `company-research`; giao diện có thể hiển thị tên trace và
tên root giống nhau. Không phải hai lần chạy nếu chỉ có một root span.
Tạo job mới với `Idempotency-Key` mới để kiểm tra; dữ liệu chưa ghi của trace cũ
không được khôi phục tự động sau khi bật cấu hình.

Langfuse dùng tracer provider riêng, không monkey-patch OpenAI hay tự bật CrewAI tracing.
Lỗi khởi tạo/start/update/end/flush telemetry không làm job thất bại hoặc gọi lại LLM.
Cuối mỗi task, worker flush span ngoài event loop với thời hạn cấu hình, còn client
được tái sử dụng trong process và SDK xử lý shutdown lúc process thoát bình thường.
Process bị kill đột ngột có thể mất trace chưa export; checkpoint/audit DB vẫn là
nguồn quyết định trạng thái job.

Log `TELEMETRY_ENABLED` chỉ xác nhận khởi tạo client, không xác nhận server đã nhận
trace. `TELEMETRY_DISABLED`/`TELEMETRY_UNAVAILABLE` ghi nguyên nhân cố định khi adapter
không hoạt động. Kiểm tra URL, key, công tắc OpenTelemetry và log exporter nếu UI
chưa có trace. Các test tự động dùng SDK thật với exporter trong bộ nhớ, không gửi
dữ liệu sang Langfuse/OpenAI.

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

Khi chạy local, JSON mẫu Acme manual với `website_text` rỗng vẫn hợp lệ: pipeline dùng
`company_description` làm evidence và không tự tải nội dung URL `website`.
`CLASSIFY` chọn nguyên văn các chuỗi evidence qua JSON Schema; validator phía server
vẫn từ chối dẫn chứng bịa hoặc diễn đạt lại.

Nếu job dừng, xem `error.code` ở `GET /api/v1/research/jobs/{job_id}` và
`details.error_category` ở endpoint `/events`. `OUTPUT_TOKEN_LIMIT_REACHED` nghĩa là
output bị cắt: cân nhắc tăng `MAX_OUTPUT_TOKENS_PER_STEP`, rồi restart worker/API.
`UNSUPPORTED_CLASSIFICATION_SIGNAL` nghĩa là signal không trích đúng evidence;
khác với lỗi thiếu key (`PROVIDER_NOT_CONFIGURED`). Mã lỗi là danh sách cố định,
không lưu raw response hoặc exception có thể chứa dữ liệu nhạy cảm.

Với các job cũ chỉ lưu `OUTPUT_OR_CONFIGURATION_INVALID`, không thể khôi phục
nguyên nhân cụ thể từ event đó. Sau khi cập nhật code/prompt, restart các tiến trình
local và tạo job mới với `Idempotency-Key` mới để kiểm tra. Không retry job có
checkpoint khác phiên bản cấu hình/prompt; thay đổi prompt `CLASSIFY` vẫn giữ nguyên
hash checkpoint `RESEARCH` nếu cấu hình khác không đổi.

SQLite WAL chỉ có một writer tại một thời điểm; hệ thống dùng transaction ngắn, busy timeout 5 giây và retry DB có giới hạn. Khi sao lưu, dùng SQLite backup API hoặc dừng service trước khi sao chép. Chỉ sao chép file `.db` khi WAL còn hoạt động có thể mất dữ liệu.

API hiện phục vụ người vận hành đáng tin cậy trên máy cục bộ và chỉ công bố cổng host ở loopback. Trước khi triển khai cho người dùng bên ngoài, cần bổ sung xác thực, phân tách tenant, quota, HTTPS và chính sách lưu giữ dữ liệu.

## 11. Giới hạn và hướng phát triển

Hệ thống **không bảo đảm gọi nhà cung cấp hoặc tính phí đúng một lần**, đặc biệt khi đã nhận response nhưng chưa kịp lưu checkpoint.

PostgreSQL đã được hỗ trợ qua `asyncpg`, với session async, khóa hàng job và bộ test recovery đồng thời chạy trên cả hai DB. Hướng dẫn chuyển cấu hình nằm tại [kiến trúc và database](architecture.vi.md). Các bước phát triển tiếp theo gồm transactional outbox để gửi queue tin cậy hơn, sổ usage nhà cung cấp, xác thực đầy đủ và giao diện duyệt các job cần kiểm tra.

Tài liệu tham khảo của README gốc: [CrewAI tasks](https://docs.crewai.com/en/concepts/tasks), [Celery periodic tasks](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html), [OpenAI GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini). Model và đơn giá trong cấu hình có thể thay đổi; cần xác nhận quyền truy cập và giá áp dụng cho tài khoản trước khi chạy thật.
