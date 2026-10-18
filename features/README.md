# Feature specifications

Thư mục này lưu đặc tả có thể dùng trực tiếp làm đầu vào triển khai.

| ID | Feature | Trạng thái | Đặc tả |
|---|---|---|---|
| F001 | Thu thập nguồn web bằng Tavily trước Research | IMPLEMENTED — 2026-10-18 | [F001-tavily-collection.md](F001-tavily-collection.md) |
| F002 | Chọn LLM provider/model theo từng step — OpenAI, Claude và Ollama | IMPLEMENTED — 2026-10-18 | [F002-per-step-llm-providers.md](F002-per-step-llm-providers.md) |
| F003 | Admin research API end to end và hướng dẫn DDD từng layer | IMPLEMENTED — 2026-10-18 | [F003-admin-api-ddd.md](F003-admin-api-ddd.md) |

## Cách yêu cầu triển khai

Ví dụ: **“Implement feature F001 trong features/F001-tavily-collection.md.”**

Khi nhận yêu cầu, đọc toàn bộ đặc tả, các AGENTS.md áp dụng và code hiện tại;
thực hiện các bước theo checklist, chạy kiểm tra phù hợp và cập nhật trạng thái.
Các đường dẫn trong đặc tả phản ánh repo tại thời điểm viết, cần đối chiếu lại trước khi sửa.
Không coi checkbox hoàn thành là bằng chứng thay thế cho code và kết quả kiểm tra.

Mỗi feature mới dùng một file `Fxxx-ten-feature.md`, có mục tiêu, hợp đồng,
thiết kế, kế hoạch triển khai, kiểm thử và tiêu chí nghiệm thu.
Nếu có nhiều feature, yêu cầu triển khai nên chỉ rõ ID hoặc tên file.
