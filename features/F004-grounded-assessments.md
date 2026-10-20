# F004 — Grounding theo nhận định và đánh giá từng tiêu chí

- Trạng thái: PROPOSED — chưa triển khai.
- Ngày tạo: 2026-10-20.
- Ưu tiên: P0; thuộc [Sprint 01](SPRINT-01-quality-and-trust.md).
- Dependency: hợp đồng rubric/version F005; release cùng F005.

## 1. Vấn đề và mục tiêu

Hiện Research có quote, Classify có matching_signals và Report có sources. Validator
kiểm tra một số quan hệ substring/source nhưng chưa ràng buộc từng field quan trọng
và từng criterion với bằng chứng cụ thể. Không thể suy từ “quote có thật” sang “kết
luận đúng”. F004 bổ sung khả năng kiểm tra và review quan hệ đó ở đường chạy v2.

Giữ số step/generation như hiện tại: COLLECT tùy chọn → RESEARCH → CLASSIFY → SCORE
→ REPORT. Không thêm một LLM evaluator vào production workflow, không tự repair.

## 2. File và ranh giới

Đọc `application/research/evidence.py`, `prompts.py`, `execute_workflow.py`,
`domain/research/outputs.py`, `policies.py`, `collection.py`, `contracts.py`,
`infrastructure/research/ai/llm_adapter.py`, `transports.py` và persistence mapping.

- Domain: output types và validation tham chiếu/quan hệ, không SDK/SQL.
- Application: tạo source excerpts, materialize ID, chọn version, tạo prompt payload.
- Infrastructure: gửi wire schema, nhận response và ghi output đã validate qua fencing.
- API: expose output đã lưu; không dựng lại claim từ DB cũ trong lúc GET.

Không tạo generic knowledge graph hoặc store riêng. Output JSON và snapshot hiện có
được ưu tiên nếu đáp ứng audit; migration chỉ thêm metadata thực sự cần cho dispatch.

## 3. Contract v2

### 3.1 Source và evidence

Manual và Tavily v2 dùng cùng một dạng sources. Operator text có source_id ổn định,
URL null; website được khai báo không biến operator text thành nội dung đã fetch.
Tavily dùng source IDs/URLs đã checkpoint. Mỗi evidence span gồm:

```json
{
  "evidence_id": "e001",
  "source_id": "operator:company_description",
  "quote": "Chúng tôi cung cấp phần mềm CRM cho doanh nghiệp nhỏ."
}
```

ID do ứng dụng cấp và có scope trong checkpoint Research của job; ổn định khi replay
cùng payload. LLM chỉ chọn ID từ candidates, không tự tạo source/quote/URL. Ứng dụng
materialize quote và source mapping từ candidates. Không dịch hay fuzzy-repair quote.
Giữ giới hạn source/text đã có; tối đa 12 evidence được chọn. Nếu không có evidence,
trả danh sách rỗng và limitations; không tự suy ra từ tên công ty/ICP.

### 3.2 Research claims

Output v2 có `schema_version: "research-v2"`, company_name, claims, evidence và
limitations. Mỗi claim có cấu trúc:

```json
{
  "claim_id": "c001",
  "field": "products",
  "value": "Phần mềm CRM",
  "evidence_ids": ["e001"]
}
```

- field thuộc `industry | products | customer_segments | business_model | geography`.
- Tối đa 12 claims, value dài 1–1200 ký tự; claim/evidence IDs không trùng.
- claim_id do ứng dụng gán theo thứ tự output đã validate; LLM không được chỉ định ID tùy ý.
- Mỗi claim phải có ít nhất một evidence ID hợp lệ. Không có nguồn thì bỏ claim,
  ghi limitation. company_name lấy từ input để định danh yêu cầu, không tuyên bố đã xác minh pháp nhân.
- Có thể tạo projection products/industry phục vụ đọc, nhưng claim là nguồn duy nhất;
  không lưu hai bản do LLM phát sinh độc lập rồi mặc định chúng luôn nhất quán.

Validators bảo đảm reference, loại dữ liệu và provenance. Chúng không chứng minh
semantic entailment. Chất lượng “value có được quote hỗ trợ không” phải được đo ở F006.

### 3.3 Classify theo rubric

Nhận rubric snapshot F005 và Research v2; trả `schema_version: "classification-v2"`
với đúng một assessment cho mỗi criterion:

```json
{
  "criterion_id": "crm_product",
  "status": "MATCH",
  "claim_ids": ["c001"],
  "reason": "Nguồn nêu rõ cung cấp phần mềm CRM."
}
```

- status là `MATCH | NO_MATCH | UNKNOWN`.
- MATCH/NO_MATCH phải có ít nhất một claim hợp lệ. NO_MATCH cần bằng chứng mâu thuẫn
  với tiêu chí, không chỉ vì không tìm thấy thông tin.
- UNKNOWN có thể không có claim; reason nói rõ thiếu dữ liệu hoặc mâu thuẫn chưa giải quyết.
- Không bỏ sót/thêm/lặp criterion; không nhận trọng số hay mandatory do LLM trả về.
- Cấm claim ID/source ID không tồn tại và criterion từ rubric khác.
- Reason là phần giải thích, không được dùng làm evidence mới cho SCORE.
- Trường hợp nguồn xung đột không giải quyết được → UNKNOWN và limitation.

Không dùng boolean icp_match làm đầu vào SCORE v2. Nếu có compatibility projection,
phải derive theo F005, ghi rõ không tương đương hoàn toàn với boolean legacy.

### 3.4 Report

V2 giữ mục đích summary, strengths, risks, next_questions, limitations nhưng mỗi
nhận định về doanh nghiệp phải tham chiếu claim_ids/criterion_ids đã lưu. Report không
nhận quyền đổi score, qualification, eligibility hoặc tạo fact mới.

Đề xuất shape của summary/strengths/risks item:

```json
{"text": "Có sản phẩm CRM được mô tả trong nguồn.", "claim_ids": ["c001"], "criterion_ids": ["crm_product"]}
```

Application đính qualification/score/coverage từ SCORE và sources từ mappings đã
validate, không lấy các số này từ văn bản LLM. `next_questions` là câu hỏi, không phải
facts; có thể gắn criterion_id cần xác minh. Limitations tối thiểu gồm loại nguồn,
độ thiếu dữ liệu, xung đột và việc chưa xác minh độc lập khi áp dụng.

Claim references tồn tại vẫn chưa bảo đảm prose không thêm ý; F006 đo lỗi này. Nếu
chưa đạt, giữ experimental hoặc dùng renderer giới hạn theo ADR, không hạ validator
để tăng completion rate.

## 4. Validation và failure behavior

Thứ tự: parse wire response → materialize evidence IDs → strict schema → reference
validation → business consistency → usage accounting → fenced checkpoint.

Output sai → NEEDS_REVIEW với code sanitize như `INVALID_CLAIM_REFERENCE`,
`INVALID_CRITERION_ASSESSMENT`, `UNSUPPORTED_REPORT_CLAIM`; dùng taxonomy hiện có nếu
đã tương đương. Không checkpoint output sai, không gọi LLM sửa. Giữ token/cost đã biết
theo cơ chế hiện tại. Không lưu raw response vào event/log chỉ để debug.

Không lẫn UNKNOWN của criterion (thiếu kiến thức nghiệp vụ) với UNKNOWN của step
(không biết kết quả request bên ngoài). Criterion UNKNOWN vẫn có thể hoàn thành job.

## 5. Version, hash và compatibility

- F005 chọn workflow/output v2 tường minh; dispatch theo snapshot của job, không theo
  settings mới nhất hoặc sự có mặt tình cờ của một key trong payload.
- Manual/Tavily legacy giữ schema/prompt/hash cũ. Không thay evidence thành objects
  ngay trong legacy `research.evidence: list[str]`.
- Identity v2 chứa domain/wire schema, prompt, candidate algorithm version và payload
  sources. Không thay thuật toán cắt quote mà giữ nguyên version/hash.
- Pending/partial job cũ không được dùng schema v2 giữa chừng. Resume step COMPLETED
  không gọi provider hoặc materialize lại bằng thuật toán mới.
- Ghi rõ giới hạn triển khai mixed-version worker; worker không hiểu version phải
  dừng an toàn trước provider I/O, không đoán fallback sang legacy.

## 6. Implementation checklist

- [ ] Định nghĩa output v2, literal versions, bounds và error taxonomy.
- [ ] Tái sử dụng candidate materialization cho manual và Tavily v2.
- [ ] Thêm claim assignment/reference validators và criterion assessments.
- [ ] Sửa prompt/wire schema cho từng provider hiện có, không tăng generation.
- [ ] Tích hợp workflow version dispatch, identity và checkpoint theo F005.
- [ ] Report references + deterministic score/source projection.
- [ ] API/admin/telemetry expose version đúng, capture content vẫn opt-in.
- [ ] Ví dụ Việt/Anh, output bị từ chối và hướng dẫn NEEDS_REVIEW.

## 7. Test và acceptance

| Nhóm | Ca bắt buộc |
|---|---|
| Grounding | ID giả, source giả, quote bị dịch, duplicate ID, empty evidence, source bị tráo |
| Claims | Claim không evidence, field ngoài enum, ID gán ổn định, value unsupported trong bộ human eval |
| Assessments | Thiếu/lặp criterion, reference sai, MATCH không evidence, NO_MATCH chỉ vì thiếu dữ liệu |
| Languages | Việt/Anh giữ nguyên quote, accents/Unicode, content có instruction độc hại |
| Report | Claim giả, score bị sửa, source URL giả, UNKNOWN được diễn đạt thành phủ định |
| Workflow | Actual adapter với HTTP mocks, SQLite/PostgreSQL, manual/Tavily, resume thêm 0 call cho checkpoint |
| Legacy | Golden schema/hash và replay job pending/partial/completed không đổi |

Acceptance kỹ thuật: 100% output được checkpoint đi qua validators; không tăng trần
request; stale worker không ghi được; invalid output ghi usage an toàn. Acceptance
ngữ nghĩa theo F006, không tuyên bố validator xác minh tính đúng của mọi claim.
