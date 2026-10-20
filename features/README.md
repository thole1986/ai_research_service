# Feature specifications

Thư mục này lưu đặc tả có thể dùng trực tiếp làm đầu vào triển khai.

| ID | Feature | Trạng thái | Đặc tả |
|---|---|---|---|
| F001 | Thu thập nguồn web bằng Tavily trước Research | IMPLEMENTED — 2026-10-18 | [F001-tavily-collection.md](F001-tavily-collection.md) |
| F002 | Chọn LLM provider/model theo từng step — OpenAI, Claude và Ollama | IMPLEMENTED — 2026-10-18 | [F002-per-step-llm-providers.md](F002-per-step-llm-providers.md) |
| F003 | Admin research API end to end và hướng dẫn DDD từng layer | IMPLEMENTED — 2026-10-18 | [F003-admin-api-ddd.md](F003-admin-api-ddd.md) |
| F004 | Grounding theo nhận định và đánh giá từng tiêu chí | PROPOSED — P0 | [F004-grounded-assessments.md](F004-grounded-assessments.md) |
| F005 | Qualification v2 theo ICP, score/coverage/eligibility | PROPOSED — P0 | [F005-icp-qualification-v2.md](F005-icp-qualification-v2.md) |
| F006 | Dataset, runner và đánh giá chất lượng trước/sau | PROPOSED — P0 | [F006-quality-evaluation.md](F006-quality-evaluation.md) |
| F007 | Cô lập test và bảo vệ API research nội bộ | PROPOSED — P0 | [F007-reproducibility-and-api-access.md](F007-reproducibility-and-api-access.md) |
| F008 | Thử nghiệm đơn giản hóa CrewAI, REPORT và Docker images | PROPOSED — P2 / stretch | [F008-simplification-study.md](F008-simplification-study.md) |

## Sprint tiếp theo

Đọc [Sprint 01 — Chất lượng quyết định và độ tin cậy](SPRINT-01-quality-and-trust.md)
trước khi chọn feature. Tài liệu có bằng chứng review, phạm vi, dependency, thứ tự
triển khai, release gates và prompt bắt đầu. F004–F008 là đặc tả đề xuất, chưa có code.

Thứ tự: **F007 test isolation → F006 baseline → F004 + F005 → F006 nghiệm thu**.
F007 auth là gate trước khi expose API; F008 là stretch, quyết định bằng số đo.
Các hợp đồng v2 được đề xuất để có thể triển khai cụ thể; job/schema/checkpoint
legacy phải được bảo toàn và v2 chưa trở thành mặc định trong sprint này.

## Cách yêu cầu triển khai

Ví dụ: **“Implement feature F001 trong features/F001-tavily-collection.md.”**

Khi nhận yêu cầu, đọc toàn bộ đặc tả, các AGENTS.md áp dụng và code hiện tại;
thực hiện các bước theo checklist, chạy kiểm tra phù hợp và cập nhật trạng thái.
Các đường dẫn trong đặc tả phản ánh repo tại thời điểm viết, cần đối chiếu lại trước khi sửa.
Không coi checkbox hoàn thành là bằng chứng thay thế cho code và kết quả kiểm tra.

Mỗi feature mới dùng một file `Fxxx-ten-feature.md`, có mục tiêu, hợp đồng,
thiết kế, kế hoạch triển khai, kiểm thử và tiêu chí nghiệm thu.
Nếu có nhiều feature, yêu cầu triển khai nên chỉ rõ ID hoặc tên file.
