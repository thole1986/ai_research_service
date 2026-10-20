# Sprint 01 — Chất lượng quyết định và độ tin cậy của company research

- Trạng thái: PROPOSED — chưa triển khai.
- Ngày lập: 2026-10-20 (Asia/Saigon).
- Đầu vào: review code và kiểm thử ngày 2026-10-20; F001–F003 đã triển khai.
- Mục tiêu: một lead được ưu tiên vì phù hợp tiêu chí có bằng chứng, không chỉ vì chứa từ khóa B2B/SaaS/SME.
- Đây là kế hoạch và đặc tả để triển khai sau; không phải bằng chứng tính năng đã tồn tại.

## 1. Kết luận và giả định sản phẩm

Giữ nền workflow hiện tại: database là nguồn sự thật, atomic claim, lease/fencing,
checkpoint, reservation, bounded attempts và UNKNOWN khi kết quả gọi bên ngoài mơ hồ.
Không viết lại backend trong sprint này.

Định hướng đề xuất: hỗ trợ nhiều ICP thông qua tiêu chí có cấu trúc do người vận hành
khai báo. Không tự biến một đoạn ICP tự do thành rubric bằng lượt LLM mới. Rubric B2B
SaaS SME là một ví dụ, không phải quy tắc chung cho mọi khách hàng.

Giữ luồng legacy cho request cũ. Phiên bản mới phải được chọn tường minh, có identity
và output version riêng; không thay nghĩa kết quả đã lưu. Đây là quyết định thiết kế
đề xuất để có thể triển khai, chưa phải xác nhận đã có nhu cầu thị trường nhiều ICP.

## 2. Bằng chứng hiện trạng

| Quan sát | Bằng chứng tại thời điểm review | Hệ quả |
|---|---|---|
| Scoring có thể HIGH khi không match ICP | Gọi `score()` với evidence B2B SaaS/SME/sản phẩm và `icp_match=false` trả 70/HIGH | Không dùng HIGH hiện tại làm quyết định bán hàng tự động |
| Score thiên về tiếng Anh | `domain/research/scoring.py` dùng regex B2B/SaaS/SME tiếng Anh | Dữ liệu Việt/Anh tương đương có thể nhận điểm khác |
| UNKNOWN chưa tách khỏi NO_MATCH | Signal thiếu bị cộng 0 trong score hiện tại | Điểm thấp có thể phản ánh thiếu dữ liệu thay vì không phù hợp |
| Grounding chủ yếu kiểm tra quote/source | `domain/research/policies.py` | Quote tồn tại chưa chứng minh mọi nhận định đúng |
| CrewAI bị giới hạn một agent/một task | `crew_factory.py`, `llm_adapter.py` | Cần đo giá trị lớp framework trước khi giữ/bỏ |
| Test phụ thuộc cấu hình môi trường | Full suite: 369 passed, 131 skipped, 3 Tavily failed; module Tavily chạy với `TAVILY_MAX_RESULTS=5`: 49 passed | Cần baseline có thể tái lập, không sửa `.env` để chiều test |
| Format check chưa đạt | Một code block trong `docs/dependency-container-guide.vi.md` | Dọn cùng hạng mục kiểm chứng |
| Provider thật chưa được xác minh trong review | Review dùng code và mocked-provider tests | Không coi số test pass là chất lượng research đã đạt |

Các con số trên là ảnh chụp của lượt review, không phải kết quả kiểm tra mới khi
triển khai sprint. Đọc lại code và chạy baseline trước khi sửa.

## 3. Phạm vi và thứ tự

| Thứ tự | Hạng mục | Ưu tiên | Dependency | Đầu ra |
|---|---|---|---|---|
| 1 | [F007 — Test isolation và bảo vệ API nội bộ](F007-reproducibility-and-api-access.md) | P0 | Không | Baseline tin cậy; request trái phép không đọc/gọi provider |
| 2 | [F006 — Bộ đánh giá chất lượng](F006-quality-evaluation.md), phần dataset/runner/baseline | P0 | Không | Đo được lỗi hiện tại trước khi sửa |
| 3 | [F004 — Grounding theo nhận định](F004-grounded-assessments.md) | P0 | Contract F005 được chốt cùng lúc | Evidence và assessment v2 có định danh |
| 4 | [F005 — Qualification theo ICP](F005-icp-qualification-v2.md) | P0 | F004 | Score/coverage/eligibility có nghĩa rõ ràng |
| 5 | F006, phần so sánh và nghiệm thu | P0 | F004, F005 | Báo cáo trước/sau, quyết định bật v2 |
| 6 | [F008 — Thử nghiệm đơn giản hóa](F008-simplification-study.md) | P2 / stretch | Baseline F006 | ADR giữ/bỏ CrewAI và REPORT LLM |

F004/F005 cùng một release contract v2; có thể phát triển tuần tự nhưng không bật
đường chạy v2 nửa chừng. F007 gồm hai phần độc lập: cô lập test làm trước; auth phải
được nghiệm thu trước khi mở API ra ngoài máy local/mạng nội bộ tin cậy.

Không cam kết cả bảng hoàn thành trong một số ngày cố định khi chưa biết số người
và năng lực triển khai. Nếu thiếu capacity, bỏ F008 trước. Nếu phần lõi vẫn quá lớn,
chia thành hai sprint: baseline + contract + F007, sau đó F004/F005 + đánh giá cuối.
Không cắt compatibility, fencing hoặc nghiệm thu chất lượng để vừa lịch.

## 4. Thứ tự triển khai cụ thể

### Giai đoạn A — Baseline và hợp đồng

- [ ] Đọc AGENTS.md áp dụng, F001–F003, schema/migration/runtime hiện tại.
- [ ] Ghi commit, working-tree state và lệnh baseline; không đưa secrets vào báo cáo.
- [ ] Cô lập test khỏi env local theo F007; chạy SQLite và lint/format.
- [ ] Tạo regression tái hiện ICP false → HIGH, thiếu dữ liệu và Việt/Anh tương đương.
- [ ] Tạo dataset, runner và baseline legacy theo F006; kết quả mock ghi rõ là mock.
- [ ] Chốt chung version dispatch, assessment schema, rubric snapshot và legacy semantics.

### Giai đoạn B — Triển khai chất lượng

- [ ] F004: source/claim IDs, structured assessments và deterministic validators.
- [ ] F005: typed rubric, pure scoring, coverage và eligibility.
- [ ] Bổ sung migration/identity trước khi bật app/worker mới nếu schema cần thay đổi.
- [ ] Giữ job cũ pending/partial/completed hoạt động theo hợp đồng cũ.
- [ ] Tài liệu API và ví dụ Việt/Anh cho manual/Tavily.

### Giai đoạn C — Kiểm chứng và release nội bộ

- [ ] F007: auth cho research endpoints, startup fail-closed và hướng dẫn local opt-out.
- [ ] Chạy bộ hồi quy SQLite; PostgreSQL/Redis chỉ trên tài nguyên test cô lập.
- [ ] Chạy đánh giá v2 trên cùng dataset/snapshot với baseline.
- [ ] Chỉ chạy provider thật qua chế độ opt-in và giới hạn chi phí của F006.
- [ ] Ghi rõ số mẫu người duyệt, số lỗi, uncertainty và ca không đạt.
- [ ] Cập nhật trạng thái feature, docs/verification.md và runbook bằng bằng chứng thật.

## 5. Những thứ chưa làm trong sprint

- Provider mới, agent delegation, tools, memory, autonomous browsing hoặc fallback nhiều model.
- Temporal, Kubernetes, event sourcing, generic BaseRepository hay framework workflow mới.
- Dashboard, CRM integration, multi-tenant SaaS, account/RBAC/SSO đầy đủ.
- Crawler tải nội dung URL, xác thực doanh nghiệp hoàn toàn hoặc bảo đảm chống mọi prompt injection.
- Tự động retry output sai bằng LLM; tự động gọi lại request có outcome mơ hồ.
- Xóa CrewAI hay đổi REPORT sang template trên đường chạy mặc định trước khi có kết quả F008.

## 6. Release gate

Sprint implementation chỉ hoàn thành khi:

1. Request/job legacy giữ idempotency, output và checkpoint semantics cũ.
2. V2 không trả HIGH nếu có mandatory NO_MATCH hoặc mandatory UNKNOWN.
3. UNKNOWN có thể phân biệt với NO_MATCH trong API và báo cáo.
4. Mỗi criterion assessment v2 truy về claim/source hợp lệ; report chỉ dùng nhận định đã cho phép.
5. Mock tests chứng minh không tăng số generation khi redelivery/resume; failure matrix cũ còn đạt.
6. Test độc lập `.env`; auth chặn trước repository/publisher/provider.
7. Có báo cáo F006 trung thực về chất lượng, chi phí và giới hạn đo.

Nếu chưa có provider thật hoặc nhãn người duyệt, có thể nghiệm thu phần kỹ thuật và
để v2 ở trạng thái opt-in/experimental. Không ghi PRODUCT-VALIDATED hoặc chuyển v2
thành mặc định chỉ vì schema tests pass.

## 7. Handover

Đầu ra cần có khi triển khai: code/tests/migrations liên quan, ví dụ request/response,
hướng dẫn rollout/rollback, báo cáo baseline và candidate, danh sách ca cần review,
và ADR F008 nếu thực hiện. Các feature chỉ được đánh dấu IMPLEMENTED sau kiểm chứng;
việc viết bộ đặc tả này không đánh dấu checklist triển khai hoàn thành.

Prompt triển khai gợi ý:

> Đọc features/SPRINT-01-quality-and-trust.md và các đặc tả được liên kết. Triển khai
> Giai đoạn A trước, bắt đầu bằng test isolation trong F007 và baseline F006. Giữ nguyên
> dữ liệu ứng dụng, không gọi provider thật, không tự nâng v2 thành mặc định. Báo cáo
> baseline và hợp đồng v2 cụ thể trước khi chuyển sang Giai đoạn B.
