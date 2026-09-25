# Kế hoạch triển khai lab L3B

## 1. Mục tiêu và nguồn yêu cầu

Hoàn thành 100 case L3B bằng workflow multi-agent có điều tra qua MCP, kết quả đúng schema V2, bằng chứng có nguồn gốc hợp lệ và trace thể hiện việc phân công, bàn giao, kiểm chứng thực tế. Gói nộp gồm `manifest.json`, `trace.jsonl` và 100 file `outputs/<case_id>.json`.

Thứ tự ưu tiên khi có xung đột: contract trong `contracts/` và quy định mới nhất trên Competition Workspace, sau đó đến `README.md` và tài liệu thiết kế của nhóm. Không lấy format 50 case của bài Day 09 khác để áp dụng cho L3B này.

Các nguồn cần kiểm tra trước khi chốt bản nộp:

- `contracts/registry/variants.json`: variant `l3b`, 100 case.
- `contracts/schemas/l3b-output-v2.schema.json`, cùng các `$defs` trong `l3a-output-v2.schema.json`.
- `contracts/schemas/mcp-evidence-response-v1.schema.json`, `trace-event-v1.schema.json`, `submission-manifest-v2.schema.json`.
- `contracts/scoring/scoring-policy-v2.json`: trọng số, hard gate, trace và call efficiency.

## 2. Quyết định kỹ thuật đã chốt

**Ngân sách model:** không dùng model ML/LLM; tổng tham số sử dụng là **0**, dưới giới hạn 10 tỷ. Các agent là vai trò Python với trách nhiệm và handoff riêng, không cần model server.

**Phân công giữa các agent:** code Python điều tra candidate, so sánh shipment/payment/customer history, dùng policy MCP để kết luận và tính tiền, giữ nguyên `evidence_ref`, ghép output, ghi trace và xác thực schema. Trace chỉ phản ánh công việc đã thực hiện.

**A2A:** các vai trò trao đổi qua cấu trúc chung có `case_id`, `task_id`, `actor`, `status`, kết quả, `evidence_refs` và `confidence`. Coordinator chỉ bàn giao dữ liệu của case hiện tại. Mỗi task có điểm kết thúc, giới hạn retry và timeout để tránh vòng lặp. Trace chỉ ghi sự kiện quan sát được; không ghi nội dung suy luận riêng.

**MCP:** dùng tool discovery và đọc tên, mô tả, schema tham số trước khi gọi. Chỉ gọi tool thuộc quyền vai trò tương ứng. Mỗi case có một evidence ledger và cache riêng, keyed theo tool + tham số; không dùng lại ref giữa các case. Mọi call đều được server audit và tính vào efficiency, kể cả call không xuất hiện trong output.

## 3. Phân công và thứ tự thực hiện

Một người có thể kiêm nhiều vai trò nếu làm cá nhân. Nhóm nên chốt cấu trúc kết quả của mỗi agent trước khi chia việc song song.

| Mốc | Chủ trì | Việc cần làm | Điều kiện hoàn thành |
| --- | --- | --- | --- |
| M0 — Quyền truy cập | Bạn/trưởng nhóm | Đăng ký team, giữ API key trong `.env`, theo dõi gói input L3B chính thức; giữ nguyên tên repo khi fork. | `day09 validate-inputs` xác nhận đúng 100 case; `day09 mcp-tools` xác thực thành công. |
| M1 — Nền tảng | Tích hợp | Cài môi trường; chốt giao diện A2A; mở rộng tool discovery để thấy tham số; tạo evidence ledger theo case, cache, timeout và giới hạn retry. | Một case giả lập chạy qua coordinator và trả kết quả có ref nguyên gốc, không gọi MCP lặp. |
| M2 — Điều tra | Entity/customer và các specialist | Triển khai entity resolution, customer context, order/product, shipment, payment/refund; ghi findings có evidence và confidence. | Candidate được chấp nhận/từ chối có lý do; timeline và số tiền được tính bằng code. |
| M3 — Quyết định | Policy/conflict + coordinator | Dùng evidence để xác định issue, trách nhiệm, refund, actions; giải quyết hoặc ghi nhận xung đột; ghép output L3B V2. | Không có claim thiếu nguồn; dữ liệu mâu thuẫn không bị chọn tùy tiện. |
| M4 — Kiểm chứng | Verifier | Thêm kiểm tra liên trường, bằng chứng, trạng thái, khoản hoàn và trace trước finalize. | Case lỗi bị chặn hoặc chuyển `needs_investigation`; output hợp lệ mới được ghi. |
| M5 — Chạy thật | Tích hợp + cả nhóm | Chạy 100 case, xem feedback tổng hợp, sửa lỗi nghiệp vụ và số call; hoàn thiện `ARCHITECTURE.md`. | Có đúng 100 output, trace đầy đủ; `day09 validate` và `day09 package` thành công. |
| M6 — Nộp | Bạn/trưởng nhóm | Kiểm tra ZIP, upload tại `/l3b`, chọn submission final. | ZIP đúng 102 entry: manifest, trace, 100 output; không chứa input, source, `.env`, key hay log. |

## 4. Chi tiết triển khai theo mốc

### M0–M1: làm ngay, chưa cần input thật

1. Cài Python 3.11+, `python -m pip install -e ".[dev]"`; chạy `pytest -q`, `ruff check .`, `day09 --help`. Repo hiện chỉ có test starter; cần bổ sung kiểm thử nghiệp vụ sau khi có cấu trúc input thực tế.
2. Chốt kiểu dữ liệu cho `AgentTask`, `AgentFinding`, `EvidenceRecord` và kết quả verifier. Mỗi đối tượng có `case_id`; findings chỉ tham chiếu ref đã nằm trong ledger của case.
3. Thiết kế coordinator gọi specialist theo dữ liệu còn thiếu, không quét toàn bộ công cụ cho mọi case. Chỉ một nơi trong code thực hiện MCP call để kiểm soát cache, số call và audit.
4. Chỉnh luồng `day09 run` để lỗi giữa chừng không làm mất toàn bộ kết quả trước đó: hiện CLI xóa output và trace ngay đầu lượt chạy. Vẫn phải bảo đảm một lượt nộp cuối dùng evidence và trace cùng run.
5. Tạo bộ case giả lập cho các tình huống ở M2–M4; ref giả chỉ dùng trong test, tuyệt đối không đưa vào bài nộp thật.

### M2–M3: logic điều tra và kết luận

- **Entity/customer:** xếp hạng order candidate theo tín hiệu từ input và MCP; ghi `resolved_order_ids`, `rejected_candidates`, `status`, confidence; chỉ truy xuất customer history sau khi có định danh hợp lý.
- **Order/product:** kiểm tra trạng thái đơn, item, seller, product và liên hệ với nội dung khiếu nại.
- **Shipment:** dựng timeline, so sánh hạn seller bàn giao với thời điểm bàn giao và giao hàng; phân biệt `seller_delay`, `logistics_delay`, `lost`, `returned`, `conflicting`.
- **Payment/refund:** đối soát captured/refunded/refundable bằng số học chính xác; phân biệt split payment hợp lệ, mismatch, duplicate capture, refund pending/failed/refunded.
- **Policy/conflict:** tra policy khi cần, xác định ưu tiên nguồn theo evidence thực tế; ghi `data_conflicts` nếu các nguồn bất đồng và để `selected_source: null` khi chưa giải quyết được.
- **Coordinator:** ghép `assessment`, `claim_assessments` khi có claim, `affected_entities`, `root_cause_analysis`, `financial_resolution`, `resolution_actions`; giảm confidence hoặc chọn `needs_investigation` khi chứng cứ chưa đủ.

### M4: các kiểm tra bắt buộc trước khi finalize

- `case_id` output và toàn bộ trace đúng case; mọi ref trong output và claim đã có trong ledger của đúng lượt chạy.
- Evidence liên quan đến claim/issue, không thêm domain thừa chỉ để tăng số ref. Evidence được dùng phải có `tool_result_consumed` tương ứng.
- Entity đã chọn không nằm trong danh sách rejected; seller, item, shipment và payment thuộc order được chọn.
- Timeline có thứ tự hợp lý; `late_seller_ids` khớp phân tích handoff.
- `captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`, `recommended_refund_brl` và tổng `refund_lines` khớp quy tắc nghiệp vụ; không hoàn trùng.
- `primary_issue`, `case_status`, trách nhiệm và actions không mâu thuẫn; confidence nằm `[0,1]` và phản ánh mức chắc chắn.
- Output pass schema V2. Trace có `case_received`, `task_assigned`, `handoff`, `verification_completed`, `case_finalized` đúng thứ tự; event `tool_result_consumed` chỉ được ghi khi dùng evidence thật.

Lưu ý: validator local kiểm tra cấu trúc JSON và một số bất biến do nhóm thêm. Quyền sở hữu ref theo **team/run/case** phải được đối chiếu với MCP audit hoặc feedback của server; không thể suy ra chỉ từ định dạng chuỗi ref.

## 5. Kiểm thử và tối ưu điểm

Tập kiểm thử tối thiểu: order ID chính xác; nhiều candidate gần giống; order không tìm thấy; seller giao trễ; logistics giao trễ; thanh toán chia kỳ hợp lệ; thu trùng; hoàn tiền chờ/lỗi; evidence mâu thuẫn; MCP timeout. Mỗi test kiểm tra kết luận, số tiền, tập evidence, hành động và trace, thay vì chỉ so JSON schema.

Khi có dữ liệu thật, chọn một nhóm case đại diện để đo số MCP call/case, tỷ lệ ambiguous, schema failure, conflict và thời gian chạy. Sau đó chạy đủ 100 case. Public feedback chỉ có điểm tổng hợp; không suy đoán oracle hay partition từ kết quả. Ưu tiên sửa hard gate, semantic và evidence trước khi tối ưu call. Scoring L3B: semantic 40%, evidence 15%, provenance 15%, consistency 10%, schema 5%, calibration 5%, workflow 5%, efficiency 5%.

## 6. Checklist trước khi nộp

- [ ] Quy định mới nhất trên Workspace và contract V2 đã được kiểm lại.
- [x] Tổng tham số model sử dụng là 0 (<10B); thiết kế được ghi trong `ARCHITECTURE.md`.
- [x] Input chính thức đúng 100 case; output đúng 100 file và đúng `case_id`.
- [ ] Không có ref giả, ref chéo case/run/team hoặc evidence bắt buộc bị bỏ sót.
- [x] Trace thể hiện các bước làm thật và liên kết được evidence với kết luận.
- [x] `pytest -q`, `ruff check .`, `day09 validate`, `day09 package --output dist/submission.zip` đều đạt.
- [x] Mở ZIP kiểm tra đúng file và giới hạn dung lượng; không chứa secret/source/input.
- [ ] Upload ZIP vào `/l3b` và chọn đúng submission final trên Workspace.
