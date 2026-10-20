# F006 — Bộ đánh giá chất lượng, chi phí và độ ổn định đầu ra

- Trạng thái: PROPOSED — chưa triển khai.
- Ngày tạo: 2026-10-20.
- Ưu tiên: P0; baseline bắt đầu trước F004/F005, candidate chạy sau hai feature này.
- Thuộc [Sprint 01](SPRINT-01-quality-and-trust.md).

## 1. Mục tiêu

Phân biệt ba loại bằng chứng: test kỹ thuật với mock, đánh giá nội dung bằng model
thật trên nguồn cố định, và smoke collection trực tiếp. Không suy chất lượng research
từ số test pass hoặc việc JSON parse thành công.

Đầu ra: dataset versioned, runner mặc định offline, baseline legacy, báo cáo candidate
v2 và quyết định bật/tạm giữ experimental. Không xây platform benchmark chung.

## 2. Dataset

Đề xuất `evals/company_research/` gồm README, manifest, cases JSONL, rubric snapshots,
source snapshots và schema. Mục tiêu 40 cases (chấp nhận 30–50 nếu có lý do ghi rõ).

Phân bổ chính, mỗi case thuộc đúng một nhóm chính để tổng không bị đếm trùng:

| Nhóm chính | Số case mục tiêu | Nội dung |
|---|---|---|
| Phù hợp rõ ràng | 8 | Nguồn đủ cho mandatory và optional |
| Không phù hợp rõ ràng | 8 | Có bằng chứng trực tiếp trái mandatory |
| Thiếu dữ liệu | 8 | Mandatory UNKNOWN, nguồn rỗng/ngắn, optional đủ nhưng mandatory thiếu |
| Mâu thuẫn/định danh | 8 | Trùng tên công ty, nguồn lẫn doanh nghiệp, nguồn trái nhau |
| Đối kháng/biên | 8 | Instruction trong nguồn, quảng cáo không đủ chứng minh, negation, dẫn quote gây hiểu nhầm |

Tag chéo: tối thiểu 10 case tiếng Việt, 10 tiếng Anh; tối thiểu 5 cặp Việt/Anh có
nội dung tương đương; ít nhất 3 ngành ngoài SaaS. Tag không phải thêm case ngoài tổng.
Các cặp ngôn ngữ và biến thể cùng công ty phải nằm cùng partition để tránh rò dữ liệu.

### Case contract

Mỗi case có:

- case_id, dataset_version, primary_group, language, tags, partition.
- company input, manual/Tavily mode và sources snapshot (nếu có).
- provenance: synthetic hoặc real-source snapshot, URL nếu có, thời điểm thu thập,
  người/nguồn tạo snapshot; không gán URL thật cho văn bản synthetic.
- rubric v2 và prose ICP; legacy baseline dùng prose tương ứng.
- expected assessments: criterion_id/status, supporting source spans, lý do.
- expected eligibility/qualification, acceptable report claims và forbidden claims.
- annotation_status: draft/reviewed; reviewer metadata nội bộ không chứa thông tin nhạy cảm.

Không để expected labels, reviewer notes hoặc forbidden claims lọt vào prompt.
Snapshot nguồn thật phải phù hợp để lưu/chia sẻ; không commit dữ liệu khách hàng,
credential hoặc nội dung cá nhân không cần thiết. Synthetic cases ghi nhãn rõ.

### Gán nhãn và phân vùng

- Chia khoảng 75% development / 25% holdout theo nhóm công ty/cặp ngôn ngữ, có
  representation của cả năm nhóm. Ghi danh sách case IDs cố định trong manifest.
- Người viết rubric tạo nhãn, người hiểu nghiệp vụ duyệt; ghi disagreement và cách xử lý.
- Không có người duyệt thì giữ draft, báo cáo là provisional, không gọi gold validated.
- Không dùng chính model đang đánh giá làm nguồn nhãn cuối cùng. LLM judge nếu có
  chỉ là trợ giúp phân tích và ngoài phạm vi mặc định sprint.
- Chỉnh prompt dựa development; holdout chỉ dùng ở gate đã chốt. Mọi thay nhãn phải có
  lý do/version mới, không đổi sau khi thấy kết quả xấu để che lỗi.

## 3. Runner và chế độ chạy

Đề xuất `scripts/evaluate_quality.py` với các mode tường minh:

1. `offline`: kiểm tra dataset, tính expected SCORE và đánh giá stored outputs;
   không import/build provider client, không cần Redis/API app/database ứng dụng.
2. `mock`: dùng workflow/adapter boundary mock, đo invariants và số generation;
   không xuất semantic quality như kết quả model thật.
3. `provider`: gọi model trên fixed snapshots, opt-in `--real-provider
   --acknowledge-cost --max-estimated-usd <positive> --max-cases <n>`.
4. Live Tavily smoke là lệnh riêng hoặc flag riêng `--live-collection`; không ngầm bật
   do case có collection_mode=tavily. Cần acknowledgment cho collection credits riêng,
   vì USD estimate của LLM không bao gồm Tavily.

Các lệnh minh họa là CLI dự kiến, chưa chạy được trước khi feature triển khai:

```powershell
.\.venv\Scripts\python scripts/evaluate_quality.py --mode offline --dataset evals/company_research --output data/evals/offline
.\.venv\Scripts\python scripts/evaluate_quality.py --mode mock --dataset evals/company_research --output data/evals/mock
.\.venv\Scripts\python scripts/evaluate_quality.py --mode provider --real-provider --acknowledge-cost --max-estimated-usd 2 --max-cases 5 --dataset evals/company_research --output data/evals/provider-smoke
```

Runner provider dùng DB/scratch riêng nếu cần workflow persistence. Không dùng
queue/DB ứng dụng, không gọi paid endpoint khi validate dataset hoặc `--help`.
Lệnh thiếu acknowledgment/cap → fail trước network. Preflight/reservation dùng policy
hiện có; không hứa hard billing cap. Unknown usage/outcome giữ reservation, dừng run
trước case tiếp theo và ghi rõ. Không auto-resubmit để lấy đủ số mẫu thành công.

Tại bước viết đặc tả này không có quyền ngầm chạy provider thật. Khi triển khai,
chỉ thực hiện chế độ có phí khi người vận hành yêu cầu rõ và cung cấp cấu hình local.

## 4. So sánh công bằng

- Baseline và candidate dùng cùng case IDs, snapshot nguồn, model/profile và giới hạn
  output nếu phù hợp. Ghi mọi khác biệt schema/prompt thay vì gọi là cùng cấu hình.
- Baseline legacy không có criterion assessments: chỉ đo metric tương thích, đánh
  dấu criterion metrics là N/A; không suy nhãn criterion giả từ điểm tổng.
- So sánh collection riêng với fixed-source reasoning; nguồn web thay đổi không được
  dùng để kết luận cải tiến prompt tốt hơn.
- Chạy một pass development để phát triển. Gate candidate có hai pass độc lập trên
  tập đã đóng băng nếu budget cho phép; mỗi pass phải là lượt chạy cố ý, ID mới,
  không phải retry im lặng. Không đủ budget thì một pass và báo thiếu đo ổn định.
- Không ghi secrets/raw prompts vào console. Stored output/source trong thư mục
  local kiểm soát; báo cáo tổng hợp trong docs không cần sao chép toàn bộ nội dung.

## 5. Metrics và cách tính

| Metric | Định nghĩa |
|---|---|
| HIGH precision | Số HIGH được nhãn người duyệt xác nhận phù hợp / số HIGH dự đoán |
| Unsafe HIGH | HIGH khi gold có mandatory NO_MATCH hoặc UNKNOWN; báo count và case IDs |
| Criterion accuracy | Đúng MATCH/NO_MATCH/UNKNOWN / tổng criterion được gán nhãn |
| UNKNOWN recall | Gold UNKNOWN được dự đoán UNKNOWN / tổng gold UNKNOWN |
| Unsupported claim rate | Claim mang nội dung thực tế không được nguồn hỗ trợ / tổng claim được người duyệt xét |
| Attribution validity | Reference/source/URL hợp lệ / tổng reference được kiểm tra |
| Completion/review rate | COMPLETED/NEEDS_REVIEW/FAILED theo toàn bộ case, không bỏ ca lỗi |
| Latency | Thời gian wall-clock end-to-end và từng step; p50/p95, ghi sample count |
| Cost | Token USD estimate/case, tổng reservation cho unknown, Tavily credits riêng |
| Stability | Tỷ lệ cùng eligibility/qualification/criterion status giữa hai pass |

Mẫu số 0 → null/N/A, không báo 100%. Semantic quality trên ca hoàn thành phải đi kèm
completion rate để tránh candidate từ chối mọi case rồi trông “ít hallucination”.
Claim/rubric findings phải có reviewer disposition; deterministic reference validity
không thay cho semantic unsupported-claim review.

## 6. Gates đề xuất

Các threshold dưới là mục tiêu sprint, không phải số đã đo hoặc cam kết thống kê:

- Toàn bộ regression deterministic và invariant mandatory gate đạt 100%.
- Trên reviewed dataset: Unsafe HIGH = 0; HIGH precision >= 90% nếu có ít nhất 10
  HIGH predictions. Ít hơn 10 → INCONCLUSIVE cho precision gate, không ép tạo HIGH.
- Criterion accuracy >= 85%; UNKNOWN recall >= 90%.
- Unsupported claim rate <= 5% trong claims được người duyệt xét; report không có
  claim sai nghiêm trọng làm thay eligibility/qualification.
- Attribution validity 100% với output đã checkpoint.
- Completion rate không giảm quá 5 điểm phần trăm so baseline trên cùng tập tương thích.
- Báo latency/cost delta; nếu tăng >25% cần lý do cụ thể và quyết định chấp nhận,
  không tự đổi model rẻ hơn giữa hai lượt để làm đẹp số.
- Stability mục tiêu >=90% cho eligibility/qualification; không đủ hai pass → chưa đo.

Dataset 30–50 nhỏ, các tỷ lệ chỉ là tín hiệu release nội bộ. Báo cả tử số/mẫu số,
holdout size và ca lỗi; không suy chất lượng toàn thị trường từ một bộ nhỏ.

## 7. Artifacts và checklist

`data/evals/<run_id>/` (không commit output nhạy cảm): manifest, config fingerprints,
results JSONL, metrics JSON, review worksheet, error summary. Không ghi credential,
provider endpoint nội bộ hoặc .env dump.

`docs/evaluation/sprint-01-report.md`: commit/dataset version, modes đã chạy,
review status, baseline/candidate table, failures theo case ID, cost/latency,
quyết định PASS/FAIL/INCONCLUSIVE và giới hạn. Đây là đường dẫn dự kiến khi triển khai.

- [ ] Dataset schema/validation + 30–50 cases + provenance + partitions.
- [ ] Expected SCORE tests và baseline legacy frozen.
- [ ] Offline/mock runner chặn network, labels không vào prompt.
- [ ] Provider/live collection flags, budget reservation và run isolation.
- [ ] Báo cáo metric đúng mẫu số, zero denominator, failed cases và nullable usage.
- [ ] Candidate F004/F005, human review và gate report.

Không cần real provider để hoàn thành runner/dataset schema. Tuy nhiên không đánh
dấu nghiệm thu chất lượng sản phẩm hoàn tất nếu provider/human review chưa thực hiện.
