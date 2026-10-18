# F002 — Chọn LLM provider và model theo từng step

- Trạng thái: IMPLEMENTED — 2026-10-18.
- Ngày tạo: 2026-10-17.
- Providers triển khai: OpenAI, Claude (Anthropic Messages) và Ollama local qua native API.
- Điều chỉnh 2026-10-18 theo yêu cầu: máy hiện tại dùng OpenAI/Claude, Ollama là tùy chọn
  để thử sau trên desktop; giữ registry mở rộng cho các provider tương lai.
- Use case chính: RESEARCH/CLASSIFY dùng OpenAI, REPORT dùng Ollama.
- Có thể triển khai độc lập hoặc sau F001; đối chiếu code F001 nếu đã triển khai.
- Đặc tả và bằng chứng triển khai được ghi dưới đây; hướng dẫn vận hành tại
  [LLM routing](../docs/llm-routing.vi.md).

## 1. Mục tiêu và quyết định phạm vi

Chọn provider/model riêng cho RESEARCH, CLASSIFY, REPORT mà không sửa business
workflow khi đổi cấu hình. Giữ nguyên schema kết quả, evidence validation, lease,
checkpoint và giới hạn số request.

```text
COLLECT (nếu có F001): Tavily, không qua LLM router
RESEARCH: profile research → OpenAI hoặc Ollama
CLASSIFY: profile classify → OpenAI hoặc Ollama
SCORE: Python, không qua LLM router
REPORT: profile report → OpenAI hoặc Ollama
```

Mỗi step có đúng một route được cố định khi tạo job. Không tự fallback provider,
không repair output bằng lượt LLM bổ sung, không gọi nhiều model để bỏ phiếu.
Không thêm agent tool loop, streaming, UI, model download, benchmark hoặc hỗ trợ
Gemini/Bedrock trong v1. Claude được bổ sung theo yêu cầu ngày 2026-10-18;
kiến trúc cho phép thêm adapter sau này.
Chọn route bằng cấu hình operator, không nhận URL/model tùy ý từ public request.

## 2. Code hiện tại và điểm cần tách

Đọc AGENTS.md áp dụng và đối chiếu các file trước khi sửa:

| Khu vực | File hiện tại | Thay đổi dự kiến |
|---|---|---|
| Settings | `src/company_ai/settings.py` | Profile, route, timeout, pricing theo provider/model |
| Port/result | `src/company_ai/domain/research/contracts.py` | Hợp đồng LLM trung lập, usage/identity |
| Workflow | `src/company_ai/application/research/execute_workflow.py` | Route snapshot, hash, cost theo step |
| Prompt | `src/company_ai/application/research/prompts.py` | Tách domain schema và provider wire schema |
| Adapter | `src/company_ai/infrastructure/research/ai/llm_adapter.py` | Tách transport OpenAI khỏi bounded Crew wrapper |
| Crew | `src/company_ai/infrastructure/research/ai/crew_factory.py` | Nhận bounded LLM trung lập provider |
| Persistence | `src/company_ai/infrastructure/research/persistence/` | Snapshot cấu hình, attempt identity, reservation |
| Wiring | `src/company_ai/runtime.py`, `src/company_ai/tasks/`, `src/company_ai/bootstrap/` | Registry/client lifecycle và config resolver |
| API | `src/company_ai/api/research/schemas.py` | Metadata route đã sanitize; giữ create payload cũ |
| Telemetry | `src/company_ai/infrastructure/telemetry/`, tests observability | Provider/model thực tế, usage và cost |

Hiện `BoundedOpenAI` trực tiếp gọi chat completions, đọc lỗi OpenAI và tự tính giá
từ một bộ rate chung. Workflow cũng tính giá lại bằng rate chung. Phải tập trung
pricing vào một policy và bỏ giả định mọi LLM step đều có settings.openai_model.

## 3. Cấu hình profile và route

Thêm một settings field JSON `LLM_ROUTING_JSON` với schema typed, extra=forbid.
JSON nằm trong .env để không thêm bind mount Docker chỉ nhằm đọc config.
Ví dụ minh họa, model Ollama là placeholder do operator thay bằng model đã cài:

```json
{
  "version": 1,
  "profiles": {
    "openai_default": {
      "provider": "openai",
      "model": "gpt-4.1-mini",
      "temperature": 0,
      "max_output_tokens": 500,
      "timeout_seconds": 90,
      "context_window_tokens": 32000,
      "pricing": {
        "kind": "estimated_token_rates",
        "input_usd_per_million": 0.40,
        "output_usd_per_million": 1.60
      }
    },
    "ollama_report": {
      "provider": "ollama",
      "model": "<installed-model-tag>",
      "temperature": 0,
      "max_output_tokens": 1000,
      "timeout_seconds": 180,
      "context_window_tokens": 8192,
      "pricing": {"kind": "local_unmetered"}
    }
  },
  "steps": {
    "RESEARCH": "openai_default",
    "CLASSIFY": "openai_default",
    "REPORT": "ollama_report"
  }
}
```

Các rates trên là giá trị config hiện có trong repo, không phải cam kết giá hiện tại;
kiểm tra rate thực tế trước khi vận hành. Không chọn sẵn model Ollama thay người dùng
vì chưa biết RAM/VRAM hoặc model họ đã cài.

- Config JSON tường minh phải chứa đủ ba routes; không fallback âm thầm khi sai tên.
- Validate provider enum, model không rỗng, số hữu hạn, timeout/context/token limits
  dương, rates không âm, output limit nhỏ hơn context limit, route tham chiếu profile có thật.
- Chỉ chấp nhận tham số v1 đã hỗ trợ; không truyền arbitrary options qua provider.
- `OLLAMA_BASE_URL` mặc định `http://localhost:11434`; thêm
  `CONTAINER_OLLAMA_BASE_URL` để Compose override cho worker nếu cần.
- URL chỉ nhận từ operator config; validate HTTP(S), không userinfo/query/fragment,
  không tự theo redirect sang endpoint khác; không đưa URL nội bộ ra public response.
- `OPENAI_API_KEY` vẫn SecretStr và chỉ cần khi profile thực thi dùng OpenAI.
- Không cấu hình OpenAI key giả để chạy Ollama và không gửi OpenAI key sang Ollama.
- Giữ các env cũ: nếu không có LLM_ROUTING_JSON, tạo ba profile/route tương đương
  cấu hình OpenAI hiện tại. Không thay default behavior của deployment cũ.
- Lease TTL phải lớn hơn timeout lớn nhất đang dùng cộng biên an toàn hiện tại (>10s).
  Validate cả snapshot cũ khi worker thực thi; không tự tăng TTL ngoài cấu hình.
- Không gọi model/provider trong import, Settings validation hoặc /ready.

## 4. Snapshot theo job và tương thích

Job mới lưu execution config snapshot có version, bất biến trong cùng transaction
tạo job/steps. Snapshot gồm resolved profile của từng LLM step, parameter/pricing,
provider adapter version, capability mode và endpoint identity không có credential.
API và worker dùng chung resolver; worker thực thi snapshot thay vì resolve lại từ env.

Endpoint identity: lưu fingerprint của URL chuẩn hóa trong snapshot và reference
provider connection; runtime phải khớp fingerprint. Đổi base URL cần job mới;
không lộ base URL qua public response. Credential được đọc lúc chạy, không snapshot;
xoay key không làm đổi hash. Nếu runtime không đáp ứng snapshot, NEEDS_REVIEW với
EXECUTION_CONFIG_UNAVAILABLE, không tự chọn model/endpoint khác.

Idempotency hash chỉ dựa trên input request như trước, không gồm server config.
Request/key cũ sau đổi env trả job cũ và snapshot cũ; muốn áp dụng config mới phải
tạo job/key mới. Không ghi đè snapshot khi request idempotent được lặp lại.

DB: thêm cột JSON nullable execution_config trên job; attempt lưu provider/model,
profile/config fingerprint, pricing kind và adapter version để audit. Migration Alembic
chỉ thêm dữ liệu/cột cần thiết; SQLite/PostgreSQL đều hỗ trợ. Giữ số liệu lịch sử.

Legacy job không có snapshot phải đi đường legacy OpenAI/hash/schema như trước;
không tự chuyển REPORT cũ sang Ollama. Không thể phục hồi chắc chắn config lịch sử
từ dữ liệu chưa lưu: nếu checkpoint hiện có không khớp settings legacy thì giữ
NEEDS_REVIEW, không đoán cấu hình. Pending legacy job tiếp tục theo cấu hình legacy.
Nếu F001 đã thêm workflow version thì giữ một nguồn version/snapshot thống nhất,
không dùng một version provider để suy ra job có COLLECT hay không.

## 5. Hợp đồng LLM và cách giữ CrewAI

Tạo kiểu request trung lập: step, trusted messages, domain output schema, resolved
execution profile. Kiểu response: raw content hoặc parsed object, provider/model,
optional request ID, optional input/output tokens, normalized finish category và
bounded provider metadata. Không lưu raw thinking/reasoning vào output báo cáo.

Các thành phần trách nhiệm rõ ràng:

1. Workflow dựng payload, prompt/schema, reserve budget và audit request intent.
2. Crew orchestrator chạy agent/task như hiện tại qua bounded BaseLLM wrapper.
3. Wrapper chỉ cho đúng một generation request mỗi attempt và gọi transport adapter.
4. Transport adapter OpenAI/Ollama chuyển request/response/lỗi về hợp đồng chung.
5. Pydantic và domain evidence validation quyết định output có được checkpoint không.

Giữ async bridge owner event loop, không tạo loop mới trong SDK call, không bật tool,
delegation, code execution, memory hoặc repair retry. Crew formatting messages không
được thay trusted messages. Test thông qua Crew thật với provider boundary mock.
Registry trong composition root ánh xạ provider ID sang adapter, tránh if/else
OpenAI/Ollama rải khắp workflow. Domain/application không import SDK provider.
Nếu sử dụng trực tiếp OpenAI SDK, khai báo dependency trực tiếp và cập nhật uv.lock;
không dựa vào dependency bắc cầu từ CrewAI.

## 6. Adapter OpenAI

- Giữ Chat Completions hiện tại và max_retries=0; không kết hợp migration Responses API.
- Dùng profile đã snapshot thay settings.openai_model/rates chung.
- Chuyển domain schema thành wire schema theo subset provider hỗ trợ; strict JSON Schema
  không được coi là hỗ trợ mọi constraint Pydantic. Validate lại local đầy đủ.
- Xử lý refusal, output bị cắt, content filter và usage thiếu như hiện tại.
- Chỉ bật tham số đã hỗ trợ cho profile; model không hỗ trợ temperature/structured
  output phải báo lỗi rõ, không tự bỏ tham số rồi gọi lại.
- Không cam kết hỗ trợ mọi model OpenAI; v1 bảo toàn model hiện tại và có test contract.

## 7. Adapter Ollama native

Dùng httpx.AsyncClient sẵn có, gọi POST `/api/chat` trên base URL operator cấu hình:

```text
model: resolved model
messages: trusted messages
stream: false
format: JSON Schema của output
options.temperature: resolved temperature
options.num_predict: max_output_tokens
options.num_ctx: context_window_tokens
```

Kiểm tra lại tên trường/options với tài liệu chính thức khi triển khai.
Đọc message.content, done/done_reason và prompt_eval_count/eval_count nếu có.
Không đọc thinking thay content; không log thinking. Chỉ chấp nhận response kết thúc
hợp lệ và output qua schema/domain validation. Thiếu terminal flag hoặc bị cắt không
được thành checkpoint thành công. Không tự bóc markdown fence hay sửa JSON sai.

- Không stream và không retry HTTP tự động; một request generation cho mỗi attempt.
- Không tự pull/download model, đổi model, dùng cloud model hoặc auto-fallback OpenAI.
- Model chưa cài trả lỗi MODEL_NOT_AVAILABLE, NEEDS_REVIEW, không auto-pull.
- Context overflow/response schema invalid là lỗi rõ; không âm thầm cắt nguồn để vừa.
- Native schema formatting và token options không bảo đảm chất lượng nội dung;
  vẫn cần validation và đánh giá thực tế với model đã chọn.
- Profile context limit được dùng cho bounded wrapper và guard đầu vào. Ước lượng
  token là heuristic có version, không hứa chính xác cho mọi tokenizer. Tránh implicit
  truncation của Ollama bằng cách kiểm tra bảo thủ và cấu hình num_ctx tường minh.
- Client được tạo/đóng trên event loop hiện tại, không cache async client qua Celery task loops.

## 8. Hash, checkpoint và model identity

LLM step hash mới gồm payload/predecessor, domain/wire schema version, prompt,
provider/model, endpoint fingerprint, options, timeout, context guard version,
adapter/capability version. Execution config fingerprint bao gồm pricing snapshot;
credential, latency và thời điểm runtime không đi vào hash.
Không thay identity SCORE hoặc COLLECT chỉ vì REPORT đổi provider.
Mọi resume kiểm tra checkpoint và snapshot như trước; manual retry giữ cùng route.
Không sửa provider/model trên một job đang chạy hoặc đã có checkpoint.

Model tag có thể trỏ sang weights khác theo thời gian. Snapshot tag là cấu hình,
không phải bằng chứng weights bất biến. Với Ollama, khi server cho phép metadata
read không generation, resolve digest model một lần trước generation đầu tiên và
lưu atomically vào execution identity riêng, không sửa request/idempotency hash.
Các attempt sau kiểm tra digest đã pin; mismatch → NEEDS_REVIEW. Digest chưa xác định
không được tự tạo; ghi identity chưa xác minh. Race khi server thay weights giữa
metadata check và generation vẫn là giới hạn cần công bố; không hứa reproducibility.
Metadata lookup không tính LLM generation call nhưng có timeout riêng, không pull model.

## 9. Usage, ngân sách và lỗi

Mọi generation, kể cả local Ollama, đều tính vào max_llm_calls_per_job. COLLECT nếu có
F001 vẫn dùng bộ đếm riêng. Ghi REQUEST_STARTED/reservation atomically trước dispatch.

- OpenAI: ước lượng/reserve và reconcile theo rates của profile/attempt snapshot.
  Thiếu usage thì giữ reservation; timeout/crash không hoàn tiền ước lượng tự động.
- Ollama local: pricing kind local_unmetered, external API estimated charge = 0;
  vẫn lưu token/call/duration. Ghi rõ không tính điện, GPU, phần cứng hoặc vận hành.
- Không áp local_unmetered cho dịch vụ Ollama cloud/remote trả phí; v1 chỉ hỗ trợ local
  hoặc endpoint tự quản được operator khai báo rõ là không tính theo API.
- Một policy duy nhất tính giá cho DB và telemetry để tránh sai lệch/nhân đôi.
- Tổng estimated_cost_usd hiện tại tiếp tục là ước lượng API charge, không tổng TCO.
- Giới hạn calls/attempts và budget admission vẫn áp dụng cho local request dù reserve=0.

| Lỗi | Hành vi |
|---|---|
| Route/profile không hợp lệ | Reject config trước khi dispatch, không âm thầm default |
| Thiếu credential/model/endpoint không khớp snapshot | NEEDS_REVIEW, không generation |
| Refusal, JSON/schema/evidence sai, truncation | NEEDS_REVIEW, không repair/fallback |
| 429 xác nhận request bị từ chối | Backoff DB hữu hạn theo policy hiện tại |
| Timeout/network error/5xx hoặc crash sau dispatch | UNKNOWN/NEEDS_REVIEW, không auto retry |
| Mất lease | Không commit kết quả; giữ cơ chế fencing |

Chuẩn hóa exception tại adapter; workflow không phụ thuộc APIStatusError.
Chỉ ghi mã lỗi allowlist, không raw provider body/exception/URL có secret.
Ollama local timeout vẫn không auto retry vì có thể generation đang chạy trên server.

## 10. API, telemetry và vận hành

Create request không cần thay đổi. GET job/steps có thêm execution metadata sanitized
cho job mới: provider, requested model, profile ID, config fingerprint, pricing kind;
giữ các field output hiện tại. Attempt events/metrics phân biệt provider/model khi
có dữ liệu; không gán provider giả cho lịch sử chưa xác định.
Langfuse generation name/provider/model, token usage và estimated cost theo route
thực tế, không gắn nhãn openai.chat.completions cho Ollama. Không log nội dung theo
mặc định; capture content vẫn opt-in, kể cả rejected output. Không đưa endpoint nội bộ,
secret hoặc thinking vào telemetry metadata/public response.

Tài liệu chạy local/Docker:

- Worker chạy trực tiếp trên host thường dùng localhost:11434.
- Worker trong Docker Desktop có thể dùng host.docker.internal:11434; hướng dẫn
  kiểm tra binding/firewall theo môi trường, không tự mở cổng/public interface.
- Ollama/model được operator cài sẵn. Không thêm Ollama container vào compose mặc định.
- Document cold start, RAM/VRAM, context và timeout; không khẳng định model nào vừa máy
  khi chưa có thông số. Config mẫu không tự tải model hay chạy generation.
- /ready giữ kiểm tra hạ tầng hiện tại; provider smoke là tác vụ tường minh, không tạo
  chi phí hoặc tải model trong health check.

## 11. Checklist triển khai theo thứ tự

- [x] Đọc repo hiện tại, F001 nếu đã triển khai, AGENTS.md và tài liệu provider chính thức.
- [x] Chốt typed config/profile/request/result/errors và chính sách compatibility.
- [x] Thêm config resolver + validation và mapping từ env legacy.
- [x] Thêm migration, snapshot bất biến lúc tạo job và attempt execution audit.
- [x] Tách bounded Crew wrapper khỏi transport OpenAI, bảo toàn behavior hiện tại.
- [x] Thêm Ollama native adapter và provider registry/lifecycle wiring; bổ sung Claude theo yêu cầu.
- [x] Đổi workflow/hash/context guard/budget policy sang profile từng step.
- [x] Cập nhật lỗi/recovery/admin retry, usage, telemetry và API metadata sanitized.
- [x] Viết tests mục 12, sửa regression và chạy check phù hợp.
- [x] Cập nhật .env.example, README, docs tiếng Việt và ví dụ routing JSON.
- [x] Ghi migration/config/restart instructions và giới hạn model identity/quality.
- [x] Cập nhật trạng thái F002/index và bằng chứng hoàn thành mục 14.

## 12. Kiểm thử bắt buộc

Tests tự động mock provider boundary; không gọi OpenAI/Ollama thật, không pull model.

- Env cũ vẫn tạo route OpenAI tương đương, payload/output manual cũ giữ tương thích.
- Typed JSON config từ chối provider lạ, routes thiếu/sai, tham số/rates/NaN không hợp lệ.
- Mixed routing dispatch đúng: RESEARCH/CLASSIFY OpenAI, REPORT Ollama.
- All-Ollama không yêu cầu OpenAI key; missing OpenAI key chỉ chặn route cần key đó.
- SCORE/COLLECT không chạy qua LLM registry và không bị gắn model LLM.
- OpenAI wire schema và Ollama format/options/stream=false đúng contract bằng mock HTTP/SDK.
- Ollama usage, done/truncation, missing model, invalid JSON, unknown usage được xử lý đúng.
- Mỗi attempt chỉ có một generation; Crew thật với mock transport không được repair thêm.
- Bằng chứng sai bị từ chối cho cả hai provider; REPORT giữ limitations/source notes trong
  prompt contract. Nội dung chính xác cần smoke/evaluation, không chứng minh bằng mock.
- Idempotent request sau đổi env trả cùng job/snapshot; job mới nhận routes mới.
- Resume/retry giữ snapshot, checkpoint không chạy lại; config unavailable/digest drift dừng.
- Legacy completed/pending/partial jobs không tự chuyển model hoặc bị hash mismatch giả.
- Rates theo từng profile, usage reconcile, missing usage giữ reserve; local reserve=0 vẫn
  bị giới hạn số call; UNKNOWN không tự release charge reservation hay fallback.
- Crash/duplicate message/stale worker tiếp tục tuân thủ claim, heartbeat, fencing.
- Telemetry model/provider/cost đúng; không key, endpoint, thinking hay raw content mặc định.
- Client đóng trước event loop, không leak/reuse sai loop; lease validation bao phủ timeout lớn.
- Migration/readback SQLite và PostgreSQL khi có môi trường, ghi rõ skipped chưa chạy.
- Nếu F001 đã có: test tavily + mixed providers end to end với provider mocks.

Chạy pytest liên quan, suite hồi quy cần thiết, ruff check, ruff format --check và
alembic check. Không báo check chưa chạy là pass.

Smoke thật là opt-in theo yêu cầu người dùng: dùng cùng checkpoint giả định hoặc job
mới, xác nhận REPORT Ollama ra JSON hợp lệ, điểm/nguồn không bị bóp méo, usage/provider
được ghi đúng. Báo trước OpenAI calls có thể tính phí. Không tự cài model hoặc tải weights.

## 13. Tiêu chí nghiệm thu và tham khảo

Hoàn thành khi có thể cấu hình mixed providers và all-Ollama, routes được snapshot,
job legacy đọc/resume đúng, không fallback/repair ngoài policy, chi phí/usage phản ánh
provider thực tế và kiểm tra bắt buộc có kết quả được ghi nhận.
Mock tests không thay thế đánh giá chất lượng model local; nếu chưa smoke thật phải
ghi rõ model/hardware thực tế chưa được xác minh.

Các nguồn chính thức đã đối chiếu khi soạn (2026-10-17), đọc lại khi triển khai:

- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs):
  JSON Schema structured output, refusal và giới hạn schema/model.
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs):
  truyền schema và validate kết quả local.
- [Ollama chat API](https://docs.ollama.com/api/chat): native chat request/response và usage.
- [Ollama FAQ](https://docs.ollama.com/faq): context, binding và môi trường vận hành.

## 14. Bằng chứng hoàn thành

Triển khai ngày **2026-10-18**, gồm Claude theo yêu cầu bổ sung của người dùng.
Hướng dẫn: [LLM routing](../docs/llm-routing.vi.md); cấu hình mẫu:
[OpenAI + Claude](../examples/llm-routing/openai-claude.json),
[OpenAI + Ollama](../examples/llm-routing/openai-ollama.json).

- Typed config/profile, endpoint fingerprint, snapshot version và pricing theo từng
  route. Env cũ tiếp tục tạo route OpenAI; all-Ollama không cần OpenAI key. Không cần
  cài Ollama để chạy OpenAI/Claude. Registry có ba transport độc lập, cho phép thêm
  adapter mà không sửa business workflow.
- Snapshot lưu cùng transaction tạo job/steps; idempotency giữ job/snapshot cũ khi
  đổi env. Legacy không snapshot giữ OpenAI/hash/schema cũ. Resume, retry, digest
  pin, lease/context/version checks không tự đổi route hoặc fallback.
- Bounded Crew giữ đúng một generation mỗi attempt. OpenAI Chat Completions,
  Anthropic Messages và Ollama native dùng schema riêng; local Pydantic/evidence
  validation giữ đầy đủ constraint. Không repair, pull model, stream hoặc bật tools.
- Audit/metrics/API metadata dùng provider/model/profile thực tế, không lộ endpoint,
  key hoặc thinking. Pricing DB và telemetry dùng chung policy; usage đã biết của
  rejected output vẫn được reconcile, timeout/crash chưa rõ giữ reservation.
- Migration **`26a1018_llm_execution_config`** thêm nullable config/identity và attempt
  audit. Chạy `alembic upgrade head` trước khi restart API/worker/Beat. Không sửa
  `.env` hoặc database/queue ứng dụng trong quá trình kiểm thử.
- Baseline trước thay đổi: **177 passed, 70 skipped**, 22,37 giây; PostgreSQL/Redis
  optional chưa bật trong baseline.
- Full suite cuối: `python -m pytest -q --disable-warnings --show-capture=no --tb=short
  --basetemp data/pytest-f002-final`, bật `TEST_POSTGRES_URL`, `RUN_REDIS_E2E=1`,
  `TEST_REDIS_URL` trỏ vào container test tạm: **431 passed, 0 skipped**, 127,57 giây.
  PostgreSQL **16.15**, Redis **7.4.11**; 1244 dependency warnings được ghi nhận.
- API → CrewAI thật → mock SDK/native HTTP → DB: mixed OpenAI/Claude và
  OpenAI/Ollama, có/không Tavily; invalid JSON chỉ một generation, usage được lưu.
  Fencing, concurrency, crash, selected-route credential, privacy và loop ownership
  đều có regression tests.
- SQLite và PostgreSQL thật: upgrade/downgrade/upgrade, `alembic check` không drift;
  replay legacy pending/partial/completed cần lần lượt **3/2/0** generation mock,
  dù cấu hình route hiện tại đã đổi sang Ollama. SQLite downgrade giữ các bảng có
  foreign key bằng native DROP COLUMN (SQLite >= 3.35).
- `ruff check .`, `ruff format --check .` (**114 files already formatted**),
  `git diff --check`, `uv --cache-dir .uv-cache lock --check --offline` và
  `docker compose config --quiet` đều pass. OpenAI SDK là dependency trực tiếp;
  lockfile hợp lệ, không đổi dependency version ngoài phạm vi cần thiết.

Không chạy smoke provider thật, không tải weights/cài Ollama; model/hardware local
và chất lượng nội dung thực tế **chưa được xác minh**. Ollama cloud endpoint/tag đã
biết bị chặn; endpoint tự quản vẫn do operator khai báo API không tính phí. Digest
metadata chưa xác định được lưu là unverified; race giữa metadata và generation vẫn
còn, không cam kết tái lập weights hoặc billing exactly-once. Chi phí là ước lượng
API charge, không gồm phần cứng/điện/vận hành.
