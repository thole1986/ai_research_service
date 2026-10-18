# Chọn provider và model cho từng bước (F002)

Mặc định cả RESEARCH, CLASSIFY và REPORT vẫn dùng OpenAI theo các biến môi trường
cũ. Máy hiện tại không cần cài Ollama. Có thể dùng Claude qua provider `anthropic`,
hoặc chuyển từng bước sang Ollama khi đã có server và model trên desktop.
COLLECT dùng Tavily; SCORE chạy Python và không đi qua registry LLM.

## Chạy ngay với OpenAI

Giữ `LLM_ROUTING_JSON=` rỗng và điền `OPENAI_API_KEY` trong `.env`. Các biến
`OPENAI_MODEL`, `LLM_TEMPERATURE`, `MAX_OUTPUT_TOKENS_PER_STEP`,
`LLM_REQUEST_TIMEOUT_SECONDS`, `INPUT_USD_PER_MILLION`, `OUTPUT_USD_PER_MILLION`
tiếp tục tạo ba route OpenAI tương đương cấu hình cũ. Không cần key Anthropic hay
kết nối Ollama. Không có provider call trong import, Settings hoặc `/ready`.

## Cấu hình route tường minh

`LLM_ROUTING_JSON` nhận một JSON version 1 có `profiles` và đủ ba `steps`.
Các profile chỉ nhận những tham số được hỗ trợ; không nhận URL hay model từ public
request. Xem ví dụ [OpenAI + Claude](../examples/llm-routing/openai-claude.json) hoặc
[OpenAI + Ollama](../examples/llm-routing/openai-ollama.json).
Các đơn giá trong ví dụ là cấu hình minh họa, không cam kết giá hiện tại; operator
cần xác nhận giá của tài khoản/model trước khi chạy.

Đọc file mẫu, sửa model/rates/limits, rồi đặt JSON một dòng vào `.env`:

```powershell
$routing = Get-Content examples/llm-routing/openai-claude.json -Raw | ConvertFrom-Json
# Xác nhận model/rates và điền ANTHROPIC_API_KEY nếu dùng route Claude.
$routing | ConvertTo-Json -Depth 20 -Compress
# Sao chép dòng JSON vừa in vào LLM_ROUTING_JSON='...'
```

Ví dụ chỉ đọc và chuyển đổi cấu hình; không tải model hay chạy generation. Các route
có thể dùng chung một profile hoặc chọn ba profile khác nhau. Để chạy all-Ollama,
trỏ cả ba route vào profile Ollama; không cần `OPENAI_API_KEY`. Key chỉ được kiểm tra
ở route sắp chạy. Nếu một route thiếu credential thì job dừng NEEDS_REVIEW, không
tự chuyển sang provider khác.

Với Claude, dùng `provider: "anthropic"`, model hỗ trợ structured outputs, pricing
`estimated_token_rates` và `ANTHROPIC_API_KEY`. Adapter dùng Messages API trực tiếp,
không bật tool, thinking hay vòng repair. Khả năng hỗ trợ phụ thuộc model và API;
model không hỗ trợ sẽ trả lỗi rõ, không bỏ tham số rồi gọi lại.
Ví dụ dùng `claude-sonnet-4-6` và temperature 1; các đời model có thể giới hạn tham
số khác nhau. Cần xác nhận model access, token/context limits và rates của tài khoản.

## Chuyển sang desktop có Ollama

Operator cài sẵn Ollama và model phù hợp RAM/VRAM. Không có Ollama container mặc
định, auto-pull, tải weights, cloud fallback hoặc benchmark tự động.

Khi desktop đã sẵn sàng, dùng `examples/llm-routing/openai-ollama.json` và thay
`<installed-model-tag>` bằng tag đã cài trước khi đưa JSON vào `.env`.

- Worker chạy trên host: thường dùng `OLLAMA_BASE_URL=http://localhost:11434`.
- Docker Desktop: đặt `CONTAINER_OLLAMA_BASE_URL=http://host.docker.internal:11434`
  trước khi tạo job mới; Compose áp dụng cùng endpoint cho API và worker.
- Kiểm tra binding/firewall trên môi trường của bạn. Cấu hình này không tự mở cổng
  hoặc thay binding của Ollama sang public interface.

Giới hạn context phải phù hợp model và phần cứng. Guard dùng ước lượng bảo thủ từ
UTF-8 bytes + schema/framing, có version; đây không phải tokenizer chính xác cho mọi
model. Request quá lớn bị từ chối nguyên vẹn, không âm thầm cắt nguồn. Ollama nhận
`num_ctx`, `num_predict`, `temperature`, `stream=false` và schema JSON. Cold start
có thể lâu; điều chỉnh timeout và đặt `LEASE_TTL_SECONDS` lớn hơn timeout lớn nhất
của các route/Tavily **trên 10 giây**. Worker cũng kiểm tra timeout của snapshot cũ.

## Migration, restart và job đang chạy

```powershell
uv sync --extra dev --frozen
.\.venv\Scripts\alembic.exe upgrade head
# Sau khi sửa .env, restart API và mọi worker/Beat đang chạy.
# Nếu dùng Docker:
docker compose up -d --build --force-recreate
```

Migration `26a1018_llm_execution_config` thêm các cột JSON config/identity và attempt audit;
không điền giả provider cho lịch sử. Job mới lưu profile, options, rates, adapter và
endpoint fingerprint trong cùng transaction tạo job/steps. API chỉ trả metadata
provider/model/profile/fingerprint/pricing; không lộ URL nội bộ hay credential.

Idempotency hash chỉ dựa trên input. Lặp lại cùng request/key trả job và snapshot
cũ, dù `.env` đã đổi. Muốn áp dụng route hoặc endpoint mới, tạo job/key mới. Resume
và manual retry giữ nguyên snapshot; xoay API key không đổi hash. Nếu endpoint,
adapter hoặc lease hiện tại không đáp ứng snapshot, job dừng
`EXECUTION_CONFIG_UNAVAILABLE`. Job legacy không có snapshot tiếp tục dùng OpenAI,
hash/prompt/schema cũ; checkpoint không khớp cấu hình legacy dừng để review.

Ollama đọc `/api/tags` với timeout riêng trước generation để pin digest khi server
cung cấp. Metadata không phải generation call. Không có digest thì lưu trạng thái
chưa xác minh; không tự tạo digest. Digest thay đổi giữa các attempt bị chặn. Tag
không bảo đảm weights bất biến, và vẫn có race nếu server thay weights giữa kiểm
tra metadata và generation. Đây không phải cam kết tái lập kết quả model.

## Ngân sách, lỗi và quan sát

Một generation tối đa cho mỗi attempt qua bounded Crew wrapper. Mọi provider đều
tính vào `MAX_LLM_CALLS_PER_JOB`, kể cả local với reserve bằng 0. OpenAI/Anthropic
reserve và reconcile theo đơn giá snapshot. Thiếu usage hoặc timeout/crash sau
dispatch giữ reservation; không tự hoàn phí hay retry. Chỉ rejection 429 rõ ràng
được backoff hữu hạn qua DB; JSON/schema/evidence sai, refusal, truncation và thiếu
model dừng để review. Không repair, vote hay fallback provider.

`local_unmetered` chỉ khai báo endpoint local/tự quản không tính API charge; không
dùng cho dịch vụ Ollama cloud có phí. Estimated cost bằng 0 không tính điện, GPU,
phần cứng hoặc vận hành. Tổng chi phí là ước lượng API charge, không phải TCO hoặc
hard billing cap.

Attempt events/metrics và Langfuse dùng provider/model thực tế, usage và cùng policy
pricing. Nội dung prompt/output chỉ capture khi opt-in; không xuất thinking, key
hay endpoint. Client tạo/đóng trên event loop của task, không cache qua Celery loops.

Usage đã biết vẫn được ghi nhận và reconcile khi JSON/schema/evidence bị từ chối;
kết quả đó không được checkpoint. Trường hợp timeout/crash chưa xác định kết quả
vẫn giữ reservation. Khi rollback migration trên SQLite, cần SQLite 3.35 trở lên
để dùng native DROP COLUMN mà không dựng lại bảng cha đang có foreign key.

## Thêm provider mới

Thêm transport thực hiện request/result trung lập trong `ai/transports.py`, đăng
ký ở `create_transports`, rồi bổ sung provider config/capability/endpoint identity và
contract tests. Business workflow, evidence validation, SCORE và COLLECT không cần
nhánh theo SDK mới. Provider cần hỗ trợ structured output được validate đầy đủ ở
local; không cam kết mọi model của một hãng đều tương thích.

Kiểm thử tự động mock provider boundary; không gọi OpenAI/Claude/Tavily/Ollama thật.
Chất lượng nội dung, model/hardware local và tính tương thích với tài khoản thực tế
cần smoke/evaluation riêng khi operator sẵn sàng. Lượt triển khai này không tải
model và chưa chạy smoke thật.

Các hợp đồng provider đã đối chiếu với tài liệu chính thức:
[OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[Claude structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
[Claude Messages](https://platform.claude.com/docs/en/api/messages/create),
[Ollama chat](https://docs.ollama.com/api/chat) và
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs).
