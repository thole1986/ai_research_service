# F007 — Cô lập kiểm thử và bảo vệ API research nội bộ

- Trạng thái: PROPOSED — chưa triển khai.
- Ngày tạo: 2026-10-20.
- Ưu tiên: P0; thuộc [Sprint 01](SPRINT-01-quality-and-trust.md).
- Hai phần độc lập: A làm trước để có baseline; B là gate trước khi expose API.

## 1. Mục tiêu và giới hạn

A: test cho cùng kết quả dù `.env` local thay đổi; không cần đọc/sửa secret người dùng.
B: người chưa xác thực không tạo job có phí hoặc đọc input/output/events của research.

Đây là access control cho dịch vụ nội bộ một trust domain. Không xây multi-tenant,
đăng nhập cá nhân, JWT, SSO hay quota/spend ledger theo khách hàng trong sprint.
Shared token không thể cung cấp tenant isolation hoặc audit theo từng nhân viên.

## 2. Phần A — Test isolation

### Hiện trạng và điều tra

Review 2026-10-20: full suite có 3 Tavily failures liên quan max_results=2 trong khi
tests kỳ vọng default 5; chạy module với env tường minh 5 có 49 passed. Fixture có
`Settings(_env_file=None)` nhưng vẫn có thể nhận process env. Chưa kết luận chính xác
điểm nạp `.env` vào process: cần tìm `load_dotenv`, imports và Celery/config side effects.

Không sửa `.env`, default production hoặc assertion thành 2 để làm test pass.

### Thiết kế

- Central fixture/builder cấp cấu hình test tường minh cho các setting ảnh hưởng
  behavior; không chỉ `_env_file=None` rồi giả định env hệ điều hành đã bị tắt.
- Chỉ sanitize các biến ứng dụng đã xác định; không xóa toàn bộ process environment
  vì có thể phá PATH, certificate, toolchain hoặc OS settings.
- `monkeypatch` với test scope/restore, hoặc settings source riêng cho test; không
  monkeypatch nội bộ Pydantic toàn cục nếu không cần.
- Environment/config parsing tests là ngoại lệ có chủ đích: tự đặt env trong test,
  kiểm tra behavior và restore; các test khác không dựa credential local.
- Không gọi dotenv hoặc tạo engine/provider/client tại module import chỉ để test
  chạy. Nếu sửa startup, giữ lifecycle và app factory invariants hiện có.
- Unit/SQLite default suite không gọi network. PostgreSQL và Redis opt-in bằng
  `TEST_POSTGRES_URL`, `RUN_REDIS_E2E`, `TEST_REDIS_URL`; không auto-detect DB/Redis ứng dụng.
- Test resource chỉ dùng DB/schema/queue namespace cô lập, dọn đúng tài nguyên test.

### Kiểm chứng

1. Chạy suite với môi trường chuẩn và `.env` local nguyên trạng.
2. Chạy subset nhạy cấu hình với process env đối nghịch: TAVILY_MAX_RESULTS=2,
   routing/model/pricing giả không có key, capture content bật. Behavior test mặc
   định vẫn theo settings test; config parsing tests vẫn đọc env như chúng yêu cầu.
3. Chạy actual Crew + HTTP mocks để phát hiện import/lifecycle side effects.
4. Chạy SQLite; PostgreSQL/Redis riêng khi có test services. Skips phải báo rõ,
   không gọi run skipped là coverage đã kiểm chứng.
5. Ruff lint/format, `git diff --check`; sửa code block format đã phát hiện trong docs.

Lệnh dự kiến vẫn dùng `.venv\Scripts\python -m pytest -q --disable-warnings --tb=short`;
ghi kết quả mới, không tái dùng số 488 trong báo cáo cũ làm bằng chứng sprint.

## 3. Phần B — API access contract

### Cấu hình

Đề xuất `RESEARCH_API_TOKEN` kiểu SecretStr và `ALLOW_UNAUTHENTICATED_RESEARCH=false`
mặc định. Token riêng ADMIN_TOKEN; không tự fallback research token sang admin token.

- Bình thường: token bắt buộc không rỗng; thiếu token → lỗi cấu hình startup rõ ràng,
  không log giá trị. Không tự mở API khi token trống.
- Opt-out chỉ khi `APP_ENV=development` và flag true tường minh; staging/production
  từ chối opt-out khi khởi động. Không suy môi trường an toàn chỉ từ địa chỉ Host header.
- Giữ admin/retry dùng ADMIN_TOKEN kể cả local opt-out. Không cho research token gọi admin.
- Thêm cấu hình vào `.env.example`, Compose env forwarding và hướng dẫn migration.
  Không tự sửa `.env` người dùng hoặc đưa token mẫu dùng được vào repo.

Đây là thay đổi có chủ đích với client/startup cũ. Ghi rõ release note và cách nâng
cấp; test settings phải cung cấp token giả hoặc opt-out local tường minh.

### Endpoint matrix

| Endpoint | Quyền sau feature |
|---|---|
| POST /api/v1/research/jobs | Research token |
| GET /api/v1/research/jobs/{job_id} | Research token |
| GET /api/v1/research/jobs/{job_id}/events | Research token |
| POST /api/v1/research/jobs/{job_id}/retry | Admin token như hiện tại |
| /api/v1/admin/* | Admin token như hiện tại |
| Health/readiness | Giữ contract hiện tại; không trả secret/input/output |

Không gắn blanket router dependency bắt cả retry phải gửi hai Bearer tokens. Dùng
dependency ở đúng routes hoặc tổ chức subrouter hợp lý. Swagger có hai security
schemes rõ ràng. API missing/wrong token → 401 `RESEARCH_AUTH_REQUIRED`,
`WWW-Authenticate: Bearer`; token đúng nhưng job không tồn tại → 404.

So sánh token timing-safe, không log Authorization. Auth dependency phải chạy trước
repository/service/publisher/provider work. FastAPI có thể đọc/parse body trước khi
dependency kết thúc: không tuyên bố auth tránh mọi chi phí parse hoặc mọi 422 cho
malformed request. Body-size middleware vẫn có quyền trả 413 trước route.

Trong shared-token scope, người có research token có thể xem các job research nếu
biết job ID. Không quảng bá đây là ownership/tenant isolation. Nếu mở cho khách hàng
độc lập, phải làm user/tenant auth, tenant-scoped idempotency và quotas ở feature sau.

### Chi phí và abuse

Auth + input/budget/attempt limits hiện có giảm rủi ro anonymous job creation, nhưng
không tạo global spending cap. Token bị lộ hoặc người có quyền gửi nhiều job vẫn có
thể phát sinh nhiều phí. Sprint không thêm rate limiter in-memory rồi tuyên bố bảo vệ
distributed workers. Để public internet ngoài trust domain cần đánh giá quota/rate
limit và global budget riêng trước rollout; không xem shared token là đủ cho SaaS.

## 4. Implementation và test matrix

- [ ] Tìm và cô lập env/import side effects, test settings builder.
- [ ] Regressions với hostile env; test không dùng local key.
- [ ] Settings validation cho token/opt-out, fail-closed production.
- [ ] Dependency/auth schemes, endpoint matrix, sanitized errors.
- [ ] Compose/examples/scripts run_demo cập nhật headers; tránh token trong command logs.
- [ ] README/docs/verification và local migration/rollback hướng dẫn.

| Test | Kỳ vọng |
|---|---|
| Missing/wrong research token, valid body | 401, zero repository/publisher/provider calls |
| Research token đúng | Create/get/events contract và idempotency không đổi |
| Research token → retry/admin | 401, không mutation |
| Admin token → retry/admin | Behavior cũ; acknowledgment/attempt/budget checks còn nguyên |
| Admin token không bằng research token → research | 401, không implicit superuser |
| Development opt-out | Research routes mở theo flag; admin vẫn auth |
| Production opt-out hoặc thiếu token | Startup fail, không fallback mở |
| Unsupported workflow version | Dừng trước provider I/O theo contract F004/F005 |
| Hostile env test settings | Default test behavior ổn định, config tests vẫn có nghĩa |
| Observability | Không token trong response/log/event/trace, capture flag không bật lén |

## 5. Acceptance và rollout

Mọi test mặc định không dùng env/secret ứng dụng làm input ngầm; lỗi được tái hiện
và sửa ở nguồn. Endpoint matrix được kiểm chứng qua HTTP với dependency counters.
Lượt kiểm chứng phải ghi SQLite/PostgreSQL/Redis đã chạy hay skip.

Rollout: cấp research token local → cập nhật clients/Compose → deploy app có auth →
kiểm tra unauthorized/authorized paths → mới mở network access. Rollback về bản
không auth chỉ trong môi trường đã đóng network access; không rollback làm mở API
ngoài ý muốn. Đặc tả không yêu cầu thay đổi firewall/deploy thật trong bước viết tài liệu.
