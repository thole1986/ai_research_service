# Chạy từng service bằng Docker Compose riêng

Mỗi service đã có một file `docker-compose.<service>.yml` tại thư mục gốc dự án. Các file này chạy độc lập bằng `-f`; không cần bật profile để chạy service được chọn.

`docker-compose.common.yml` chứa cấu hình ứng dụng dùng chung, không phải file để khởi động. `docker-compose.yml` là file tổng hợp, tham chiếu lại các file riêng bằng `extends` để tránh hai bộ cấu hình bị lệch nhau.

## 1. Danh sách file

| Service | File Compose | Chức năng |
|---|---|---|
| Migration | [docker-compose.migrate.yml](../docker-compose.migrate.yml) | Chạy Alembic một lần |
| API | [docker-compose.api.yml](../docker-compose.api.yml) | FastAPI và Swagger tại cổng host 8000 |
| AI worker | [docker-compose.ai-worker.yml](../docker-compose.ai-worker.yml) | Xử lý queue `ai` |
| Maintenance worker | [docker-compose.maintenance-worker.yml](../docker-compose.maintenance-worker.yml) | Xử lý queue `maintenance` |
| Beat | [docker-compose.beat.yml](../docker-compose.beat.yml) | Lập lịch tác vụ bảo trì |
| Verification | [docker-compose.verification.yml](../docker-compose.verification.yml) | Lint, format check và pytest |
| Redis tùy chọn | [docker-compose.redis.yml](../docker-compose.redis.yml) | Redis riêng cho máy chưa có Redis |
| PostgreSQL tùy chọn | [docker-compose.postgres.yml](../docker-compose.postgres.yml) | PostgreSQL local |

Dockerfile và script `.sh` của từng service được giữ nguyên. File [docker-compose.postgres-test.yml](../docker-compose.postgres-test.yml) vẫn dành riêng cho PostgreSQL test tạm thời, không phải database ứng dụng.

## 2. Chuẩn bị một lần

Chạy các lệnh từ thư mục gốc dự án. Nếu chưa có `.env`, sao chép `.env.example`; không ghi đè `.env` đang dùng:

```powershell
Copy-Item .env.example .env
```

Tự điền key, admin token và cấu hình DB/broker trong `.env`. Mặc định vẫn dùng SQLite và Redis bên ngoài ở host port 6379. Không cần chạy file Redis tùy chọn nếu bạn đã có Redis.

Xem [cấu hình SQLite/PostgreSQL và pool](architecture.vi.md) nếu cần đổi database.

## 3. Chạy riêng từng service theo đúng thứ tự

**File riêng chỉ chứa một service, không tự chạy migration hoặc service khác.** Vì vậy cần chuẩn bị Redis/database trước, chạy migration thành công, rồi mới chạy API và các worker.

### Bước 1 — migration

```powershell
docker compose -f docker-compose.migrate.yml run --rm --build migrate
```

Đợi lệnh kết thúc thành công rồi tiếp tục. `--rm` xóa container migration sau khi chạy, không xóa volume hay database. Chạy lại migration khi cập nhật schema; không cần chạy mỗi lần chỉ restart API.

Nếu migration thất bại, sửa cấu hình/kết nối/schema trước; các file service riêng không có dependency tự chặn bạn khởi động tiếp.

### Bước 2 — API

```powershell
docker compose -f docker-compose.api.yml up -d --build
```

Swagger: http://localhost:8000/docs.

### Bước 3 — AI worker

```powershell
docker compose -f docker-compose.ai-worker.yml up -d --build
```

### Bước 4 — maintenance worker

```powershell
docker compose -f docker-compose.maintenance-worker.yml up -d --build
```

### Bước 5 — Beat

```powershell
docker compose -f docker-compose.beat.yml up -d --build
```

### Kiểm tra

```powershell
docker compose ps -a
Invoke-RestMethod http://localhost:8000/ready
```

API nhận job và AI worker xử lý pipeline; maintenance worker + Beat cần hoạt động để quét lease hết hạn, tiếp tục job chờ và dọn event cũ. Không coi API healthy là bằng chứng các worker/Beat đều đang chạy.

Sau lần build đầu, có thể bỏ `--build` khi không có thay đổi mã nguồn/dependency. Nếu sửa `.env`, dùng `up -d --force-recreate` với file service tương ứng để tiến trình nhận cấu hình mới.

## 4. Xem log, restart hoặc dừng một service

Ví dụ với API:

```powershell
docker compose -f docker-compose.api.yml logs -f --tail=100 api
docker compose -f docker-compose.api.yml restart api
docker compose -f docker-compose.api.yml stop api
```

Đổi file và tên service để thao tác worker hoặc Beat. `restart` không cập nhật biến môi trường; sau khi sửa `.env` cần tạo lại container:

```powershell
docker compose -f docker-compose.api.yml up -d --force-recreate api
```

Muốn bỏ container đã dừng của một service mà vẫn giữ dữ liệu:

```powershell
docker compose -f docker-compose.api.yml stop api
docker compose -f docker-compose.api.yml rm -f api
```

Dùng `stop`/`rm` khi chỉ thao tác một service. Các file chia sẻ network nên `down` trên một file riêng có thể báo network vẫn đang được service khác sử dụng.

## 5. Chạy Redis hoặc PostgreSQL tùy chọn

### Redis

Bạn đã có Redis bên ngoài nên có thể bỏ qua mục này. Nếu cần Redis của project:

```powershell
docker compose -f docker-compose.redis.yml up -d --build --wait
```

Redis tùy chọn công bố host port `6380` mặc định. Để các container ứng dụng sử dụng Redis này, đặt trong `.env`:

```dotenv
CONTAINER_REDIS_URL=redis://redis:6379/0
```

Sau đó tạo lại API, worker và Beat để nhận cấu hình. Tên `redis` hoạt động vì các service dùng chung Docker network.

### PostgreSQL

Đặt `.env` cho các container ứng dụng:

```dotenv
DB_BACKEND=postgresql
DATABASE_URL=
CONTAINER_DATABASE_URL=
CONTAINER_POSTGRES_HOST=postgres
POSTGRES_DB=company_ai
POSTGRES_USER=company_ai
POSTGRES_PASSWORD=<mat-khau-local-cua-ban>
POSTGRES_PORT=5432
```

Khởi động PostgreSQL và chờ sẵn sàng:

```powershell
docker compose -f docker-compose.postgres.yml up -d --build --wait
```

Sau đó chạy migration và các service ứng dụng theo mục 3. PostgreSQL dùng volume `postgres-data`; cổng container là `5432`, cổng host mặc định là `5433`. Container ứng dụng kết nối tên `postgres`, cổng nội bộ `5432`.

Nếu dùng PostgreSQL bên ngoài, bỏ qua file này và cấu hình `CONTAINER_POSTGRES_HOST`/DSN theo [hướng dẫn database](architecture.vi.md).

## 6. Chạy kiểm thử riêng

```powershell
docker compose -f docker-compose.verification.yml run --rm --build verification
```

Mặc định các test cần PostgreSQL/Redis thật được skip nếu chưa bật biến tương ứng. Test LLM luôn mock; không cần OpenAI API key.

Để kiểm tra Redis bên ngoài:

```powershell
docker compose -f docker-compose.verification.yml run --rm --build -e RUN_REDIS_E2E=1 -e TEST_REDIS_URL=redis://host.docker.internal:6379/15 verification
```

Để bật cả PostgreSQL test, chuẩn bị database test riêng rồi truyền thêm `-e TEST_POSTGRES_URL=postgresql+asyncpg://...`. Không dùng database chứa dữ liệu thật làm môi trường thử migration. Xem [hướng dẫn test hai DB](architecture.vi.md#9-kiểm-thử-hai-database).

## 7. Vì sao chạy file riêng vẫn dùng chung dữ liệu?

Tất cả file service và file tổng hợp có cùng project name mặc định:

```text
ai_research_service
```

Vì vậy chúng cùng tham chiếu:

| Tài nguyên | Tên mặc định |
|---|---|
| Network | `ai_research_service_default` |
| Volume ứng dụng/SQLite | `ai_research_service_app-data` |
| Volume Redis tùy chọn | `ai_research_service_redis-data` |
| Volume PostgreSQL tùy chọn | `ai_research_service_postgres-data` |

Tên mặc định giữ tương thích với dữ liệu của project ở thư mục hiện tại. Đổi `COMPOSE_PROJECT_NAME` hoặc dùng `-p` sẽ chọn nhóm tài nguyên khác; dữ liệu cũ không tự bị xóa nhưng có thể trông như một DB mới/trống.

Nếu muốn đặt tên project khác, dùng **cùng một giá trị cho mọi file và mọi lệnh**, kể cả migration, Redis và PostgreSQL. Không đặt một project name riêng cho từng service khi chúng cần chia sẻ SQLite hoặc gọi nhau qua service name.

Khi chạy một file riêng, Compose có thể cảnh báo các container của service còn lại là “orphan” vì chúng không nằm trong file đang chọn. Đây không phải yêu cầu xóa chúng. **Không dùng `--remove-orphans` khi bật/tắt một service riêng**, vì các service khác trong cùng project có thể bị xóa.

## 8. Vẫn có thể chạy hoặc dừng cả stack

File tổng hợp vẫn giữ dependency migration và các profile tùy chọn như trước:

```powershell
docker compose up -d --build
```

Lệnh mặc định chạy migrate, API, hai worker và Beat; không tự khởi động Redis/PostgreSQL tùy chọn.

Dừng toàn bộ service thuộc project, kể cả các service tùy chọn đã chạy từ file riêng:

```powershell
docker compose --profile "*" down
```

Không thêm `-v` nếu muốn giữ dữ liệu. Redis bên ngoài thuộc project khác không bị tác động.

## 9. Kiểm tra cấu hình mà không khởi động container

```powershell
docker compose -f docker-compose.api.yml config --quiet
docker compose -f docker-compose.ai-worker.yml config --quiet
docker compose --profile "*" config --quiet
```

Trong lần tách cấu hình, cả 8 file riêng đã được kiểm tra bằng `docker compose config`; cấu hình từng service khớp file tổng hợp sau khi loại trừ dependency/profile chỉ dành cho chạy chung. Project name, network và volume SQLite cũng đã được đối chiếu. Không khởi động container để thực hiện thay đổi này.
