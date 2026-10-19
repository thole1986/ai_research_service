# Dẫn chứng RESEARCH trong Tavily mode

Từ prompt `collected-evidence-v3`, application chia nội dung nguồn thành các đoạn
nguyên văn có mã định danh. Structured output yêu cầu model chọn `evidence_ids`
trong enum của request, thay vì tự viết lại câu trích và source ID.

Application ánh xạ các ID về `evidence` và `evidence_sources` trước khi validate
domain và lưu checkpoint. API vẫn trả cấu trúc cũ. Không sửa dấu câu, so khớp gần
đúng, chuyển dẫn chứng sang nguồn khác hoặc gọi thêm LLM để sửa output.

Mỗi đoạn tối đa 600 ký tự; câu dài được chia thành các đoạn liên tiếp. Nguồn có
quá nhiều câu ngắn được chia toàn bộ thành các đoạn có giới hạn để tránh enum quá
lớn. Mã đoạn chỉ có ý nghĩa trong đúng payload nguồn của request đó.

Validator vẫn kiểm tra nguyên văn và nguồn, kể cả với provider không tuân thủ
schema. Log `STEP_STOPPED` có thể kèm `evidence_reason`:

- `QUOTE_NOT_IN_SOURCE`: đoạn trích không xuất hiện trong nguồn đã chọn.
- `UNKNOWN_SOURCE_ID`: nguồn không tồn tại.
- `EVIDENCE_MAPPING_MISMATCH`: danh sách evidence và mapping không khớp.
- `DUPLICATE_SOURCE_ID`: nguồn bị trùng ID.
- `UNKNOWN_EVIDENCE_ID`: model chọn mã đoạn không tồn tại hoặc sai kiểu dữ liệu.

Các mã này không chứa nội dung nguồn. Dẫn chứng nguyên văn không tự chứng minh
nguồn đúng thực tế hay công ty đã được xác minh độc lập.

Sau khi cập nhật, restart/rebuild API và worker, rồi tạo job với Idempotency-Key
mới. Job cũ có checkpoint/prompt hash khác không nên dùng để kiểm chứng bản sửa.

## Tái hiện MISA bằng provider thật

Script này dùng key và cấu hình trong `.env`, phát sinh phí API. Không tự chạy
trong bộ test. Nó kiểm tra riêng COLLECT/RESEARCH, không tạo job trong DB và không
chạy CLASSIFY/SCORE/REPORT.

```powershell
.venv/Scripts/python.exe scripts/repro_misa.py
.venv/Scripts/python.exe scripts/repro_misa.py --crew
```

Lần đầu gọi Tavily rồi OpenAI. Các lần sau dùng lại nguồn trong
`data/misa-repro.json` và chỉ gọi OpenAI, giúp so sánh trên cùng bộ nguồn.
`--crew` kiểm chứng qua CrewAIAdapter. File `data/` không được Git theo dõi.

Khi kiểm chứng ngày 19/10/2026, model đã đổi dấu phẩy cuối đoạn trích thành dấu
chấm và bị validator từ chối. Sau sửa, 4/4 dẫn chứng khớp nguyên văn và nguồn;
lần gọi trực tiếp dùng 152 output token, lần qua CrewAIAdapter dùng 185 output
token. Đây là kết quả smoke cho một bộ nguồn, không phải cam kết chất lượng mọi job.
