# Kiến trúc DDD, async và cấu hình database

## 1. Đánh giá bản trước và thay đổi hiện tại

Bản trước chia thư mục theo tầng nhưng chưa tách DDD rõ: application dùng dữ liệu ORM và phụ thuộc trực tiếp hạ tầng. Bản hiện tại áp dụng DDD ở mức thực dụng cho một bounded context **nghiên cứu và đánh giá doanh nghiệp**.

| Tầng | Vị trí | Trách nhiệm |
|---|---|---|
| Domain | `src/company_ai/domain/` | Đối tượng `ResearchJob`, `ResearchStep`, sự kiện, hợp đồng đầu ra, quy tắc retry, ngân sách, nguồn dẫn chứng và chấm điểm |
| Application | `src/company_ai/application/` | Use case tạo/đọc/retry job, workflow, heartbeat, recovery; định nghĩa repository/publisher port |
| Infrastructure | `src/company_ai/infrastructure/` | SQLAlchemy async repository, ánh xạ ORM ↔ domain, kết nối DB, CrewAI/OpenAI adapter và Celery publisher |
| API | `src/company_ai/api/` | HTTP, Pydantic request/response, mã lỗi và xác thực quản trị; gọi use case application |
| Composition root | `main.py`, `runtime.py`, `tasks/` | Lắp ghép dependency, quản lý lifecycle API và cầu nối task Celery |

Domain không import SQLAlchemy, FastAPI, CrewAI hoặc Redis. Application phụ thuộc domain và các port, không import implementation hạ tầng. ORM model chỉ nằm trong infrastructure; repository chuyển kết quả thành đối tượng domain trước khi trả về. `tests/unit/test_architecture.py` bảo vệ chiều phụ thuộc này.

Đây không phải kiến trúc event sourcing hay một hệ thống nhiều bounded context. Sự kiện hiện tại là audit bền vững trong DB; dữ liệu đầu vào/đầu ra của một job vẫn được checkpoint theo từng bước.

## 2. Luồng async

- API handler và use case dùng `async def`/`await`.
- DB dùng `create_async_engine`, `async_sessionmaker`, `AsyncSession`; SQLite qua `aiosqlite`, PostgreSQL qua `asyncpg`.
- Mỗi thao tác repository tạo session và transaction riêng; không chia sẻ một `AsyncSession` giữa các coroutine chạy đồng thời.
- Sau khi commit claim hoặc ghi nhận bắt đầu request, session đóng trước khi chờ LLM. Heartbeat là một asyncio task độc lập với session riêng.
- OpenAI dùng `AsyncOpenAI` với `max_retries=0`; Crew chạy bằng `await crew.akickoff()`.
- CrewAI 1.15.28 vẫn gọi một hook đồng bộ từ thread của executor. Adapter chuyển hook về event loop sở hữu `AsyncOpenAI` bằng `run_coroutine_threadsafe`; không tạo một event loop mới cho mỗi request provider. Cổng giới hạn một request được giữ nguyên.
- Celery vẫn yêu cầu task entrypoint đồng bộ. `runtime.run_async` tạo event loop cho lần thực thi task, tạo engine trong phạm vi đó, chạy use case async và `await dispose()` trước khi đóng loop. Không cache engine async xuyên task/loop/process.
- API có engine riêng, được giải phóng trong lifespan khi dừng ứng dụng. Redis healthcheck dùng client async. Vì Celery publish là API đồng bộ, publisher đưa thao tác này vào thread để không chặn event loop API.

Hàm thuần như tính điểm, tạo hash và quyết định nghiệp vụ vẫn là hàm đồng bộ vì không có I/O cần chờ.

## 3. SQLite để phát triển local

Trong `.env`:

```dotenv
DATABASE_URL=
CONTAINER_DATABASE_URL=
DB_BACKEND=sqlite
```

Mặc định dùng `sqlite+aiosqlite:///./data/ai_research.db`. Trên host, file nằm tại `data/`; trong container, thư mục làm việc là `/app`, nên file nằm trong volume tại `/app/data`.

Có thể chỉ định đường dẫn khác:

```dotenv
DATABASE_URL=sqlite+aiosqlite:///./data/custom.db
```

URL cũ `sqlite:///...` vẫn được chuẩn hóa sang driver async để tương thích cấu hình cũ. Trong Docker, dùng `CONTAINER_DATABASE_URL` nếu cần ghi đè đường dẫn container.

SQLite vẫn dùng WAL, foreign key, busy timeout 5 giây và retry có giới hạn cho lỗi khóa. Async không loại bỏ giới hạn chỉ một writer của SQLite.

## 4. PostgreSQL bên ngoài — giống cách cấu hình service của bạn

Không cần đưa mật khẩu vào một chuỗi f-string. Settings sử dụng `URL.create` để xử lý đúng các ký tự như `@`, `:`, `/`, `?`, `#` trong username/password.

```dotenv
DATABASE_URL=
CONTAINER_DATABASE_URL=
DB_BACKEND=postgresql
POSTGRES_HOST=localhost
POSTGRES_DB=company_ai
POSTGRES_USER=company_ai
POSTGRES_PASSWORD=<mat-khau-cua-ban>
POSTGRES_PORT=5432
CONTAINER_POSTGRES_HOST=host.docker.internal
DB_POOL_MODE=pooled
DB_POOL_SIZE=16
DB_MAX_OVERFLOW=48
DB_POOL_RECYCLE=3600
DB_POOL_TIMEOUT=30
```

Khi chạy trực tiếp trên host, kết nối dùng `POSTGRES_HOST`. Compose thay host bằng `CONTAINER_POSTGRES_HOST` để container truy cập PostgreSQL bên ngoài; các biến database/user/password/port còn lại lấy từ `.env`.

Driver tạo ra là `postgresql+asyncpg`. Không cần đổi ORM sang SQLModel để dùng `AsyncSession`; các model SQLAlchemy 2 hiện tại vẫn sử dụng được.

Nếu đã có DSN hoàn chỉnh, có thể dùng:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/company_ai
CONTAINER_DATABASE_URL=postgresql+asyncpg://user:password@host.docker.internal:5432/company_ai
```

DSN hoàn chỉnh có độ ưu tiên cao hơn các biến thành phần. Vì vậy, muốn chọn DB bằng `DB_BACKEND`, cần để trống URL tương ứng. Nếu tự viết DSN, bạn phải URL-encode ký tự đặc biệt trong thông tin đăng nhập; dùng các biến thành phần sẽ tránh công việc này.

Sau khi cấu hình:

```powershell
uv sync --extra dev --frozen
.\.venv\Scripts\alembic upgrade head
# Hoặc chạy toàn bộ stack container:
docker compose up -d --build
```

## 5. Pool và NullPool

| Cấu hình | Hành vi |
|---|---|
| `DB_POOL_MODE=pooled` | PostgreSQL tái sử dụng connection trong engine hiện tại; có `pool_pre_ping`, size, overflow, recycle và timeout |
| `DB_POOL_MODE=null` | Dùng `NullPool`, không giữ connection để tái sử dụng sau khi session trả connection |
| Alembic | Dùng engine async với `NullPool`, dispose sau migration |
| SQLite | Không áp dụng các tham số pool size/overflow của PostgreSQL; vẫn hỗ trợ chế độ `null` |

Mỗi engine chỉ chọn một chế độ pool. Không tạo đồng thời hai engine toàn cục có cấu hình khác nhau khi không có use case riêng. Giới hạn pool áp dụng theo engine/process, không phải tổng connection của toàn bộ hệ thống; điều chỉnh theo số worker và giới hạn PostgreSQL của bạn.

Trong `infrastructure/database/session.py`, `Database.get_session()` là async generator có thể dùng cho DI của các endpoint CRUD ngắn. Pipeline hiện dùng session theo thao tác repository để bảo đảm không giữ transaction qua cuộc gọi LLM.

## 6. Migration và tương thích dữ liệu

Schema tiếp tục được quản lý bằng Alembic, không gọi `create_all` khi API/worker khởi động. `create_all` chỉ xuất hiện trong fixture test để dựng schema cô lập.

```powershell
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\alembic current
.\.venv\Scripts\alembic check
```

Migration đầu tiên vẫn được giữ nguyên. Revision tiếp theo chuyển timestamp PostgreSQL sang kiểu có múi giờ, diễn giải dữ liệu timestamp cũ là UTC. Với SQLite, dữ liệu vẫn lưu UTC theo quy ước cũ và được đọc thành datetime có timezone.

Chuyển cấu hình từ SQLite sang PostgreSQL sẽ kết nối sang một database khác; **không tự chuyển dữ liệu cũ**. Cần một quy trình xuất/nhập riêng nếu muốn mang job hiện có sang DB mới.

## 7. Giữ invariant trên cả hai database

Trên SQLite, transaction ghi được tuần tự hóa bởi DB. Trên PostgreSQL, các thao tác thay đổi một job lấy khóa hàng job trước rồi mới thay đổi step/attempt, giúp tránh cập nhật mất ngân sách và giảm deadlock do thứ tự khóa không nhất quán.

Claim và completion vẫn dùng conditional update. Completion yêu cầu đúng lease token, input hash, trạng thái `RUNNING` và lease chưa hết hạn. Quét lease dùng cập nhật có điều kiện; LLM có kết quả không rõ vẫn thành `UNKNOWN` và cần duyệt.

Chỉ retry transaction cho lỗi SQLite bị khóa hoặc PostgreSQL báo deadlock/serialization failure đã rollback. Không tự replay transaction khi lỗi kết nối/commit có kết quả không xác định. Không có cuộc gọi LLM nào nằm trong callback transaction có thể được replay.

## 8. PostgreSQL local tùy chọn

Nếu chưa có PostgreSQL, có thể bật service tùy chọn. Mỗi service vẫn có Dockerfile và script `.sh` riêng.

Đặt `.env`:

```dotenv
DB_BACKEND=postgresql
DATABASE_URL=
CONTAINER_DATABASE_URL=
CONTAINER_POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=company_ai
POSTGRES_USER=company_ai
POSTGRES_PASSWORD=<mat-khau-local-cua-ban>
```

```powershell
docker compose --profile bundled-postgres up -d --build --wait postgres
docker compose up -d --build
```

Container PostgreSQL dùng cổng nội bộ `5432`, công bố host port `5433` mặc định và lưu vào volume `postgres-data`. Nếu chạy Python trên host với PostgreSQL này, dùng host `localhost`, port `5433` cho cấu hình host hoặc DSN host; container ứng dụng vẫn phải kết nối cổng nội bộ `5432`.

## 9. Kiểm thử hai database

```powershell
docker compose -p company-ai-db-test -f docker-compose.postgres-test.yml up -d --build --wait
$env:TEST_POSTGRES_URL = 'postgresql+asyncpg://company_ai_test:local-test-only@localhost:55432/company_ai_test'
$env:RUN_REDIS_E2E = '1'
.\.venv\Scripts\pytest -q --basetemp=data/pytest-async
docker compose -p company-ai-db-test -f docker-compose.postgres-test.yml down
```

Redis E2E dùng Redis bên ngoài ở `localhost:6379/15` mặc định; đổi `TEST_REDIS_URL` khi cần. Test PostgreSQL tạo schema ngẫu nhiên riêng rồi xóa schema đó, không thao tác lên bảng nghiệp vụ có sẵn. Tài khoản test cần quyền tạo/xóa schema. Container PostgreSQL test dùng tmpfs riêng, không dùng volume ứng dụng.

Không có `TEST_POSTGRES_URL` thì các case PostgreSQL được đánh dấu skip. Không có `RUN_REDIS_E2E=1` thì case broker thật được skip. LLM luôn được mock trong test tự động, bao gồm kiểm thử CrewAI thật với SDK async được mock.

Tham khảo API chính thức đã kiểm tra: [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html), [Alembic asyncio](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic).
