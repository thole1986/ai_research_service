# F008 — Thử nghiệm đơn giản hóa CrewAI, REPORT và Docker images

- Trạng thái: PROPOSED — STRETCH; chưa triển khai.
- Ngày tạo: 2026-10-20.
- Ưu tiên: P2; chỉ làm khi P0 của [Sprint 01](SPRINT-01-quality-and-trust.md) đủ capacity.
- Dependency: baseline F006; không chặn release chất lượng v2.

## 1. Câu hỏi cần quyết định

1. CrewAI có đem lại lợi ích đo được cho một agent/một task không tools/memory/delegation,
   trong khi provider transport và structured output đã được adapter riêng kiểm soát?
2. REPORT có cần một generation riêng nếu phần lớn dữ liệu đã có trong checkpoints?
3. Các app service có thể dùng chung image mà giữ được command/lifecycle độc lập không?

Đầu ra bắt buộc là ADR có bằng chứng và quyết định. Đây không phải yêu cầu xóa ngay
CrewAI, thay REPORT mặc định hoặc gom tất cả service thành một process.

## 2. Nhánh thử nghiệm và giới hạn

Thực hiện prototype cục bộ/branch riêng hoặc trong eval runner; không thêm hàng loạt
settings runtime và không để backend experimental trở thành mặc định production.
Giữ production path cho đến khi có feature triển khai tiếp theo được nghiệm thu.

### A. Direct structured adapter

- Gọi lại transport/schema/validation/usage hiện có qua LLMAdapter protocol.
- Một generation/attempt, không fallback/repair, không SDK retries ẩn.
- Giữ async ownership, cancellation/timeout classification, safe failure, unknown
  outcome và sanitized errors. Không bypass request_started/reservation/fencing.
- Dùng cùng prompts/wire schemas/profiles với Crew path để so sánh hợp lý.
- Prototype phải chứng minh adapter errors được accounting như cũ; không chỉ demo happy path.

### B. Report template

- Renderer nhận validated checkpoints, xuất facts/criterion statuses/score/coverage,
  limitations, source references và câu hỏi cho UNKNOWN.
- Không tự tạo facts, revenue estimates hay recommendations vượt rubric.
- So sánh tính dễ đọc bằng reviewer worksheet F006; nguồn/score đều materialize deterministic.
- Không tự khai báo REPORT là deterministic trong job hiện tại: nếu chọn triển khai
  sau ADR, cần workflow version mới, execution_type, hash, cost/call-budget và routing
  semantics rõ ràng. F002 đang yêu cầu route REPORT; không âm thầm bỏ qua cấu hình đó.

### C. Shared app image

- So sánh Dockerfile API/AI worker/maintenance/Beat/migrate, phân biệt app image với
  PostgreSQL/Redis images. Không gom các DB/broker vào app image.
- Thử một app image dùng command/entrypoint khác nhau, giữ service boundaries và
  migration dependencies trong Compose.
- Đo build context/layers/cached rebuild, image sizes, startup health và quyền non-root.
- Không thay toàn bộ Compose service files chỉ để giảm số file; các standalone service
  được giữ nếu đang phục vụ workflow của người dùng.

## 3. Ma trận so sánh

| Tiêu chí | Crew + LLM report | Direct + LLM report | Direct + template report |
|---|---|---|---|
| Generation thành công/job | Đo thực tế | Đo thực tế | Đo thực tế |
| Checkpoint/redelivery calls | Completed steps thêm 0 | Phải đạt tương đương | Phải đạt tương đương |
| Invalid output/timeout/crash | Baseline | Không mất invariant | Không mất invariant |
| Unsupported claims, HIGH precision | F006 | Cùng dataset | Cùng dataset + report review |
| p50/p95 và token estimate | Baseline | Delta | Delta |
| Dependencies/bridge code | Liệt kê có căn cứ | Phần bỏ/giữ | Phần bỏ/giữ |
| Dễ đọc và hành động được | Reviewer | Reviewer | Reviewer |

Không dùng số dòng code hoặc warning count làm tiêu chí duy nhất. Prototype ít code
nhưng mất accounting, cancellation hoặc provenance thì không được chọn.

Không có provider budget thì chỉ benchmark mock/runtime overhead và deterministic
renderer. Ghi rõ chưa chứng minh semantic quality/real latency; không suy template
hay direct tốt hơn thực tế từ kết quả mock.

## 4. Decision criteria

- Chỉ đề xuất thay Crew khi direct path giữ đầy đủ invariants và giảm chi phí bảo trì
  cụ thể (dependency, lifecycle bridges, error handling), không làm chất lượng giảm.
- Template phù hợp khi đáp ứng report content/readability trên reviewed cases và
  không cần generation để tổng hợp có ích. Có thể quyết định giữ LLM nếu reviewer
  thấy lợi ích rõ ràng; ghi tradeoff chi phí/unsupported prose.
- Shared image phù hợp khi giữ standalone startup, migration order, healthchecks,
  non-root và lockfile reproducibility, với giảm duplication đo được.
- Không có bằng chứng đủ → DEFER, không phải mặc định xóa framework.

## 5. Deliverables và acceptance

- [ ] ADR `docs/decisions/0001-research-runtime-simplification.md` (đường dẫn dự kiến).
- [ ] Baseline/candidate commits, profile/dataset IDs, commands và bảng số đo.
- [ ] Danh sách invariant tests chạy và failure findings.
- [ ] Quyết định riêng cho Crew, report renderer và image; KEEP/CHANGE/DEFER với lý do.
- [ ] Nếu CHANGE: scope feature tiếp theo, version/migration/rollout/rollback và rủi ro.

Done của F008 là quyết định có thể review, không phải production refactor. Không
đánh dấu dependency CrewAI đã xóa hoặc cost/job đã giảm trước khi thay đổi được triển khai.
