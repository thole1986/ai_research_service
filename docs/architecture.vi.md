# Kiến trúc DDD, async và cấu hình database

## 1. Đánh giá bản trước và thay đổi hiện tại

Bản hiện tại áp dụng DDD cho bounded context **nghiên cứu và đánh giá doanh nghiệp**, chia package theo context trong từng tầng. Trước refactor DI, `create_app()` tạo một repository và `JobService` dùng chung trên `app.state`; factory còn cần truy cập `repo.db` nên fake repository phải biết chi tiết hạ tầng. Hiện factory chỉ cấu hình HTTP và đăng ký dependency; service/repository được resolve theo request.

| Tầng | Vị trí | Trách nhiệm |
|---|---|---|
| Domain | `src/company_ai/domain/research/` | Đối tượng `ResearchJob`, `ResearchStep`, sự kiện, hợp đồng đầu ra, quy tắc retry, ngân sách, nguồn dẫn chứng và chấm điểm |
| Application | `src/company_ai/application/research/` | Use case tạo/đọc/retry job, workflow, heartbeat, recovery; định nghĩa repository/publisher port |
| Infrastructure | `infrastructure/research/persistence/`, `infrastructure/research/ai/` | ORM, repository, ánh xạ ORM ↔ domain và CrewAI/OpenAI adapter của research |
| Hạ tầng dùng chung | `infrastructure/database/`, `messaging.py`, `telemetry/` | Engine, session factory, transaction retry, Celery publisher và telemetry |
| API | `api/research/`, `api/admin/`, `api/health/` | HTTP, request/response schema và gọi use case qua `Depends` |
| Composition root | `bootstrap/`, `main.py`, `runtime.py`, `tasks/` | Gắn concrete dependency, quản lý lifespan API và cầu nối task Celery |

Domain không import SQLAlchemy, FastAPI, CrewAI hoặc Redis. Application phụ thuộc domain và các port, không import implementation hạ tầng. ORM model chỉ nằm trong infrastructure; repository chuyển kết quả thành đối tượng domain trước khi trả về. `tests/unit/test_architecture.py` bảo vệ chiều phụ thuộc này.

Đây không phải kiến trúc event sourcing hay một hệ thống nhiều bounded context. Sự kiện hiện tại là audit bền vững trong DB; dữ liệu đầu vào/đầu ra của một job vẫn được checkpoint theo từng bước.

## 2. Luồng async

- API handler và use case dùng `async def`/`await`.
- DB dùng `create_async_engine`, `async_sessionmaker`, `AsyncSession`; SQLite qua `aiosqlite`, PostgreSQL qua `asyncpg`.
- API: mỗi request nhận một `AsyncSession` qua `Depends(get_session)`. Các dependency trong cùng request tái sử dụng session theo cơ chế cache của FastAPI. `SessionOperations` kết thúc transaction sau mỗi thao tác research; dependency đóng session khi request kết thúc.
- Worker: mỗi thao tác repository tạo session/transaction riêng. Sau khi commit claim hoặc ghi nhận bắt đầu request, session đóng trước khi chờ LLM. Heartbeat là một asyncio task độc lập với session riêng. Không chia sẻ `AsyncSession` giữa các coroutine chạy đồng thời.
- OpenAI dùng `AsyncOpenAI` với `max_retries=0`; Crew chạy bằng `await crew.akickoff()`.
- CrewAI 1.15.28 vẫn gọi một hook đồng bộ từ thread của executor. Adapter chuyển hook về event loop sở hữu `AsyncOpenAI` bằng `run_coroutine_threadsafe`; không tạo một event loop mới cho mỗi request provider. Cổng giới hạn một request được giữ nguyên.
- Celery vẫn yêu cầu task entrypoint đồng bộ. `runtime.run_async` tạo event loop cho lần thực thi task, tạo engine trong phạm vi đó, chạy use case async và `await dispose()` trước khi đóng loop. Không cache engine async xuyên task/loop/process.
- Engine và Redis client của API chỉ được tạo khi vào lifespan và được giải phóng khi thoát lifespan, kể cả khi có lỗi. Import `main` hoặc gọi `create_app()` không tạo engine. Vì Celery publish là API đồng bộ, publisher đưa thao tác này vào thread để không chặn event loop API.

Hàm thuần như tính điểm, tạo hash và quyết định nghiệp vụ vẫn là hàm đồng bộ vì không có I/O cần chờ.

### Dependency graph và cách mở rộng

```text
research/admin router
  → Depends(get_job_service)
      → Depends(get_research_repository) [application port]
          → get_sqlalchemy_research_repository [bootstrap binding]
              → Depends(get_session)
                  → Depends(get_database) [engine/session factory của lifespan]
      → Depends(get_job_publisher) [Celery adapter]
```

API định nghĩa dependency contract trong `api/research/dependencies.py`; `bootstrap/dependencies.py` gắn implementation bằng `app.dependency_overrides`. API không import SQLAlchemy hoặc infrastructure. Router vẫn dùng DI như sau:

```python
from typing import Annotated
from fastapi import Depends
from company_ai.api.research.dependencies import get_job_service
from company_ai.application.research.jobs import JobService

@router.get("/{job_id}")
async def get_job(
    job_id: str,
    service: Annotated[JobService, Depends(get_job_service)],
):
    return await service.get(job_id)
```

`bootstrap.dependencies.get_session` là provider `AsyncSession` dùng chung để gắn repository của domain mới. HTTP handler inject application service; session được inject ở provider concrete repository, giúp giữ ORM ngoài presentation và các tầng nghiệp vụ.

Khi thêm campaign, tạo `domain/campaign`, `application/campaign`, `infrastructure/campaign/persistence` và `api/campaign`. Định nghĩa port của campaign, provider service ở API và provider repository tại composition root với `Depends(get_session)`; đăng ký binding trong `configure_dependencies()` và router trong `api/router.py`. Alembic cần import model của context mới để đưa vào `Base.metadata`. Không cần sửa DB/session factory hay repository research. Admin hiện là nhóm endpoint quản trị research, chưa có nghiệp vụ riêng để tạo một bounded context admin.

Repository bám theo aggregate research: job, step, attempt và event được cập nhật cùng transaction để giữ lease/checkpoint invariant. Chưa cần `BaseRepository[Model]` kiểu CRUD vì nó không biểu đạt các thao tác nghiệp vụ này; domain mới có thể có repository riêng mà vẫn dùng chung engine/session provider.

`SessionOperations` sở hữu transaction của từng thao tác research, yêu cầu session đang idle; không bọc nó trong transaction bên ngoài hoặc chạy song song trên một session. Use case tương lai cần một transaction bao trùm nhiều repository phải định nghĩa Unit of Work riêng thay vì dùng các thao tác research tự commit. Dependency `get_session` chỉ quản lý lifetime, không tự commit ở cuối request. Nhờ vậy job đã commit trước khi publish, và read transaction đã rollback trước khi chuyển sang write hoặc chờ broker.

### Override dependency trong test

`create_app(settings)` thay thế chữ ký cũ nhận `repo`, `enqueue`, `broker_ping`. Override port hoặc provider cụ thể theo nhu cầu:

```python
from company_ai.main import create_app
from company_ai.api.research.dependencies import get_research_repository

app = create_app(settings)

async def fake_repository():
    return fake_repo  # Chỉ cần application port, không cần thuộc tính .db

app.dependency_overrides[get_research_repository] = fake_repository
```

Override `get_job_publisher`, `get_broker_ping` và `get_database_ready` độc lập; hai readiness dependency trả về callable async. Để chạy repository production trên DB fixture, override `bootstrap.dependencies.get_database`: fixture sở hữu việc dispose và lifespan không tạo thêm engine. Test client phải chạy lifespan. Giữ các binding mặc định khi thay override; nếu gọi `app.dependency_overrides.clear()`, gọi lại `configure_dependencies(app)` trước request.

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

`bootstrap/dependencies.py:get_session` là dependency HTTP lấy session từ factory trong `infrastructure/database/session.py`. API research thực sự sử dụng provider này qua dependency graph. Worker dùng session theo thao tác repository để bảo đảm không giữ transaction qua cuộc gọi LLM.

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
