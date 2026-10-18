# 04 — Bootstrap và DI: nối hợp đồng với database

API yêu cầu một reader nhưng không tự chọn SQLAlchemy. Bootstrap là nơi nối yêu
cầu đó với implementation và cấp session đúng thời gian sống của request.

[api/admin/dependencies.py](../../src/company_ai/api/admin/dependencies.py) khai báo
`get_admin_reader()` như contract provider; gọi trực tiếp khi chưa wiring sẽ báo
chưa cấu hình. `get_admin_service(reader)` tạo `ResearchAdminService(reader)`;
alias `Admin` viết gọn `Annotated[ResearchAdminService, Depends(get_admin_service)]`.
Port không phải adapter, và Depends không tự viết/chạy SQL.

[bootstrap/dependencies.py](../../src/company_ai/bootstrap/dependencies.py) có
`configure_dependencies(app)`: override `get_admin_reader` bằng
`get_sqlalchemy_admin_reader(session)`, trả `SqlAlchemyAdminReader(session)`.
Constructor chỉ giữ session, chưa checkout connection hoặc đọc job Sen.
Khi layer trên gọi `await service.get_job_detail(id)`, adapter mới thực hiện SELECT
và trả DTO `JobDetail`; JSON được map ở API.

```python
# Cấu hình thật trong configure_dependencies:
app.dependency_overrides[get_admin_reader] = get_sqlalchemy_admin_reader
# Provider implementation:
return SqlAlchemyAdminReader(session)
```

Đồ thị resolve là `Admin → get_admin_service → get_admin_reader [override] →
get_sqlalchemy_admin_reader → get_session → get_database`. Đây là đồ thị dependency,
không phải SQL chạy lần lượt trong từng constructor. Bootstrap được phép import cả
API contract/application và infrastructure; API/application không import ngược lại.

[lifespan.py](../../src/company_ai/bootstrap/lifespan.py) tạo engine và Redis client
khi app bắt đầu phục vụ, đóng khi app dừng hoặc startup thất bại. `create_app` không
tạo database. Test override `get_database` tự sở hữu engine nên lifespan không tạo
engine thay thế. Redis client được tạo nhưng admin GET không ping/publish tới Redis.

`get_session` dùng `async with database.session()` rồi `yield session`: mỗi request
có session riêng. FastAPI cache dependency trong một request, nên hai nhánh Depends
cùng `get_session` nhận cùng object; request tiếp theo nhận object khác. Adapter
dùng `SessionOperations.session()` để rollback read autobegin trước khi trả DTO;
dependency đóng session cả khi success, lỗi hoặc cancellation. Không auto-commit.

`await session.execute(...)` nhường event loop trong lúc chờ DB. Nó không tự tạo
worker, không chạy đồng thời nhiều query trên cùng session, không giữ transaction
trong lúc gọi network/provider. Frozen DTO đã deepcopy JSON nên serialize được sau
khi session đóng; ORM không ra khỏi infrastructure.

Để test nghiệp vụ HTTP mà thay reader:

```python
async def fake_reader():
    return reader_stub


app.dependency_overrides[get_admin_reader] = fake_reader
```

Để test HTTP→DB thật, chỉ override `get_database` bằng fixture-owned database như
[conftest.py](../../tests/conftest.py); giữ reader/service production. Khi debug lỗi
"dependency is not configured", kiểm tra override bằng **function object** đúng module,
không ghi string tên function. Khi pool còn checkout, kiểm tra scope yield/rollback
và caller có giữ session ngoài request không.

Validation phase này: [test_admin_dependencies.py](../../tests/integration/test_admin_dependencies.py),
[test_dependency_injection.py](../../tests/integration/test_dependency_injection.py),
architecture và lifespan tests đạt **12 passed** trên SQLite/PostgreSQL thật. Test
admin binding chứng minh construction không phát SQL và service nhận reader thay thế.
