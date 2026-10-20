# F005 — Qualification v2 theo ICP, tách fit khỏi độ đầy đủ bằng chứng

- Trạng thái: PROPOSED — chưa triển khai.
- Ngày tạo: 2026-10-20.
- Ưu tiên: P0; thuộc [Sprint 01](SPRINT-01-quality-and-trust.md).
- Dependency: F004 assessments; F006 cho nghiệm thu chất lượng.

## 1. Vấn đề

`scoring.py` hiện cho 40 điểm B2B/SaaS, 30 ICP, 20 SME và 10 sản phẩm. Có thể HIGH
70 dù icp_match=false. API cho nhập ICP tự do nhưng scoring chỉ phù hợp một nhóm ICP;
regex tiếng Anh và cách cho 0 điểm khi thiếu thông tin làm sai lệch diễn giải.

Mục tiêu v2: SCORE là hàm thuần nhận rubric snapshot và assessments có bằng chứng.
Không dò từ khóa tiếng Anh hoặc dùng tên sản phẩm do LLM trả để chấm điểm substring.
Không thêm generation để dịch ICP hay tự chọn trọng số.

## 2. Request contract và phạm vi

Thêm field optional `qualification_profile` vào JobInput. Không truyền → legacy.
Có truyền → bắt buộc contract dưới đây, chọn luồng v2 cho toàn bộ job:

```json
{
  "company_name": "Lotus CRM Demo",
  "company_description": "Chúng tôi cung cấp phần mềm CRM cho doanh nghiệp nhỏ.",
  "icp": "Nhà cung cấp CRM cho doanh nghiệp nhỏ tại Việt Nam",
  "qualification_profile": {
    "version": "icp-v2",
    "profile_id": "crm-sme-vn",
    "criteria": [
      {"id": "crm_product", "description": "Cung cấp sản phẩm phần mềm CRM", "weight": 50, "mandatory": true},
      {"id": "sme_customer", "description": "Khách hàng mục tiêu là doanh nghiệp nhỏ và vừa", "weight": 30, "mandatory": false},
      {"id": "vn_market", "description": "Có phục vụ thị trường Việt Nam", "weight": 20, "mandatory": false}
    ]
  }
}
```

Rubric là định nghĩa scoring có thẩm quyền trong v2; `icp` vẫn là mô tả ngữ cảnh để
giữ request dễ đọc và tương thích. Không trích tiêu chí bổ sung từ prose `icp`, không
cho prose thay weight/mandatory. Docs yêu cầu người gửi giữ prose và rubric nhất quán;
không tuyên bố có validator tự hiểu mọi mâu thuẫn ngôn ngữ giữa chúng.

Ràng buộc:

- profile_id/id: 1–64 ký tự `[a-z0-9_-]`; criterion IDs duy nhất.
- criteria: 1–8; description trim, 1–300 ký tự, điều kiện đủ cụ thể để người duyệt đánh giá.
- weight: số nguyên 1–100, tổng đúng 100; mandatory là boolean strict.
- Ít nhất một mandatory criterion để HIGH luôn có điều kiện nghiệp vụ rõ ràng.
- Version ngoài hỗ trợ, unknown fields, duplicate ID, tổng sai, NaN/coercion sai → 422.
- Tính tất cả text của profile vào giới hạn input; tăng giới hạn phải là cấu hình
  tường minh, không bỏ profile khỏi bộ đếm hoặc body-size middleware.
- profile không chứa provider/model/URL, prompt system hay điều khiển retry.

Các giới hạn trên là thiết kế đề xuất cho v2, cần được phản ánh thống nhất trong
Pydantic schema, domain model, prompts, docs và examples khi triển khai.

## 3. Công thức SCORE

Input gồm rubric đã validate và đúng một assessment mỗi criterion từ F004.
Gọi M là tổng weight MATCH, N là tổng weight NO_MATCH, U là tổng weight UNKNOWN.
Luôn M + N + U = 100. Không làm tròn trung gian.

```text
coverage_percent = M + N
score = null nếu coverage_percent == 0
score = floor(100 * M / coverage_percent + 0.5) nếu coverage_percent > 0
```

Score là tỷ lệ fit trong phần đã biết, không phải xác suất mua hàng, độ tin cậy của
model hoặc tỷ lệ được kiểm chứng độc lập. Vì score có thể cao khi coverage thấp,
API/report bắt buộc trình bày coverage và eligibility cùng score.

Eligibility và qualification theo thứ tự ưu tiên:

| Điều kiện | eligibility | qualification |
|---|---|---|
| Có mandatory NO_MATCH | INELIGIBLE | LOW |
| Không mandatory NO_MATCH nhưng có mandatory UNKNOWN | UNDETERMINED | INSUFFICIENT_EVIDENCE |
| Tất cả mandatory MATCH nhưng coverage < 70 | ELIGIBLE | INSUFFICIENT_EVIDENCE |
| Tất cả mandatory MATCH, coverage >= 70, score >= 70 | ELIGIBLE | HIGH |
| Tất cả mandatory MATCH, coverage >= 70, 40 <= score < 70 | ELIGIBLE | MEDIUM |
| Tất cả mandatory MATCH, coverage >= 70, score < 40 | ELIGIBLE | LOW |

Thresholds cố định trong `rules_version="icp-weighted-v2"` của sprint; không thêm
settings điều chỉnh không snapshot. Mọi thay đổi công thức/threshold cần version mới.
Không gán score=0 khi tất cả UNKNOWN; không gán icp_match=false chỉ vì chưa đủ nguồn.

## 4. Output

```json
{
  "schema_version": "score-v2",
  "rules_version": "icp-weighted-v2",
  "profile_id": "crm-sme-vn",
  "profile_fingerprint": "<digest>",
  "score": 100,
  "coverage_percent": 80,
  "eligibility": "ELIGIBLE",
  "qualification": "HIGH",
  "rule_breakdown": [
    {"criterion_id": "crm_product", "weight": 50, "mandatory": true, "status": "MATCH", "claim_ids": ["c001"]},
    {"criterion_id": "sme_customer", "weight": 30, "mandatory": false, "status": "MATCH", "claim_ids": ["c002"]},
    {"criterion_id": "vn_market", "weight": 20, "mandatory": false, "status": "UNKNOWN", "claim_ids": []}
  ],
  "limitations": ["Chưa có bằng chứng về thị trường Việt Nam."]
}
```

Đây là output shape minh họa cho assessments tương ứng, không phải kết quả khẳng
định từ request ví dụ ở mục 2. F004/F005 không bảo đảm LLM tạo claim c002 nếu nguồn
không có thông tin đó. Breakdown dùng trọng số từ rubric, không từ provider output.
Null score được API/admin/report bảo toàn, không convert sang 0 hoặc gọi float(null).

Không thay enum/int contract ScoreOutput legacy thành v2 trên mọi job. Dùng schema
dispatch tường minh theo workflow version; consumers phải đọc schema_version.

## 5. Snapshot, idempotency và rollout

- Canonical rubric nằm trong input snapshot của job mới. Fingerprint gồm version,
  profile_id và criteria theo thứ tự khai báo; thứ tự có ý nghĩa trình bày, giữ nhất quán
  trong canonical request/hash. Không dựa riêng profile_id để nhận dạng quy tắc.
- Cùng idempotency key + cùng payload canonical → job cũ; đổi rubric/weight/mandatory
  hoặc chọn v2 với key cũ → 409, không tái dùng điểm đã tính cho rubric khác.
- Request legacy không có profile phải giữ request hash cũ; không đưa field default
  null mới vào canonical payload rồi làm hỏng idempotency F001/F002.
- Job lưu workflow/output version rõ ràng. Ưu tiên mở rộng cơ chế version hiện có;
  kiểm tra độ dài cột và invariant trước khi thêm migration.
- Input hash RESEARCH/CLASSIFY/SCORE/REPORT v2 chứa đúng schema/prompt/rules và rubric
  liên quan. SCORE identity chứa toàn rubric, assessments và rules_version.
- Legacy checkpoints không được sửa/xóa/backfill sang v2. Muốn đánh giá lại thì tạo job
  mới, key mới, có chi phí được ghi nhận như lượt thực thi mới.
- Deploy migration → app/worker hiểu cả legacy và v2 → opt-in v2. Không chạy worker cũ
  chung queue khi worker đó có thể claim version mới mà không kiểm tra hỗ trợ.
- Rollback: ngừng nhận v2 trước, xử lý/drain job v2 với worker hỗ trợ hoặc giữ an toàn;
  không rollback về binary không đọc được rows mới rồi để nó tự xử lý chúng.

## 6. Test matrix

| Case | Kết quả bắt buộc |
|---|---|
| Tất cả MATCH | score 100, coverage 100, ELIGIBLE/HIGH |
| Tất cả UNKNOWN | null, coverage 0, UNDETERMINED/INSUFFICIENT_EVIDENCE |
| Mandatory NO_MATCH, các criterion khác MATCH | INELIGIBLE/LOW dù score số học cao |
| Mandatory UNKNOWN, optional MATCH | UNDETERMINED/INSUFFICIENT_EVIDENCE |
| Mandatory MATCH 50, optional UNKNOWN 50 | score 100, coverage 50, INSUFFICIENT_EVIDENCE |
| Mandatory MATCH 50, optional NO_MATCH 50 | score 50, coverage 100, MEDIUM |
| Coverage 69/70 và score 39/40/69/70 | Đúng boundary, deterministic rounding |
| Evidence Việt/Anh cùng ý nghĩa và assessments giống nhau | SCORE giống tuyệt đối |
| Ngoài domain SaaS: sản xuất/logistics/retail | Không tự cộng B2B/SaaS/SME; chỉ dùng rubric |
| Không có claim, missing criterion, duplicate criterion | Reject trước SCORE checkpoint |
| Đổi config/rubric sau tạo job | Snapshot cũ không đổi |
| Duplicate task, SCORE crash, stale fencing | Không tăng generation, không overwrite checkpoint |
| Legacy pending/partial/completed | Semantics/hash cũ, resume 3/2/0 fake calls khi phù hợp fixture |

SCORE unit tests kiểm tra nghiệp vụ; không viết test chỉ lặp từng dòng implementation.
Việt/Anh semantic equivalence ở CLASSIFY phải đo thêm F006 vì pure scorer không hiểu ngôn ngữ.

## 7. Checklist và acceptance

- [ ] Typed rubric + strict request validation + input-size accounting.
- [ ] Version/canonicalization/snapshot và migration nếu cần.
- [ ] Pure score_v2 và output contract nullable/versioned.
- [ ] CLASSIFY assessments F004 → SCORE → REPORT integration.
- [ ] API/admin examples, hướng dẫn diễn giải coverage và eligibility.
- [ ] Compatibility/recovery/budget tests trên SQLite/PostgreSQL.
- [ ] Bộ đánh giá F006 cho ICP false/HIGH, tiếng Việt và ngành ngoài SaaS.

Done khi các invariant trong mục 6 đạt và HIGH không vượt eligibility/coverage gate.
Chưa đạt chất lượng provider thật thì giữ opt-in, ghi rõ giới hạn; không chỉnh nhãn
gold hoặc bỏ case khó để làm đẹp số đo.
