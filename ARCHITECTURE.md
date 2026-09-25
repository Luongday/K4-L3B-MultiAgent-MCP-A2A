# L3B Architecture Record

## Workflow

`day09 run` đọc 100 input L3B và xử lý tuần tự. Coordinator giao việc cho evidence, entity, order, shipment, payment, policy, conflict và verifier agents qua thông điệp A2A có `case_id` và `task_id`. Các handoff và evidence đã dùng được ghi vào trace. Chỉ evidence-agent gọi MCP; mỗi case có ledger riêng, cache theo tool và tham số, trần cứng 16 call. `evidence_ref` trong output chỉ lấy từ MCP response của case đó.

CLI ghi từng case đã xong vào `.day09-run/` (trace trước, output sau) và chỉ thay `outputs/` + `traces/trace.jsonl` khi đủ 100 case qua schema và kiểm tra bất biến. Mỗi MCP call có timeout 60 giây. Lỗi nghiệp vụ của tool (ví dụ order không tồn tại) được ghi vào ledger; lỗi kết nối hoặc timeout không bị nuốt mà làm coordinator mở lại phiên MCP và chạy lại case đó từ đầu (tối đa 4 lần). Nếu lượt chạy bị ngắt, `day09 run --resume` tiếp tục từ các case đã stage của chính lượt đó.

## Ngân sách tham số

Workflow không gọi model ML/LLM. Các vai trò là agent Python có task, handoff và evidence riêng; policy-agent áp dụng quy tắc lấy từ MCP. Tổng tham số model sử dụng là **0**, dưới giới hạn 10 tỷ.

## Kế hoạch gọi MCP

Input có `customer_request.claimed_order_id`, `candidate_order_ids`, `customer_unique_id_hint`, hai claim (một topic nghiệp vụ và `requested_full_refund`) và `policy_version`. Tool được chọn qua discovery theo domain (tên, mô tả, input schema), không hard-code danh sách tool.

1. `get_customer_history` theo customer hint.
2. `get_order` cho claimed order trước. Khi order vừa được `get_order` xác nhận vừa nằm trong lịch sử của đúng khách hàng, entity được xem là kiểm chứng độc lập và các candidate còn lại bị loại mà không gọi thêm. Nếu không, các candidate khác mới được kiểm tra.
3. `get_policy` và các domain theo topic của claim: item, payment và product luôn cần; shipment chỉ cho claim giao hàng và `unsupported_claim`; refund cho claim hoàn tiền và đơn hủy/không khả dụng; seller cho claim quy trách nhiệm seller. Payment dùng một tool timeline (chứa cả payment rows và lifecycle events) thay vì gọi hai tool trùng dữ liệu.

Kết quả: 6–8 call mỗi case thay vì 11, không gọi tool mà claim không cần.

## Phân giải dữ liệu mâu thuẫn

Nguồn thật có hai giao dịch cùng `order_id` ở các ngày khác nhau, và `get_order` có thể trả giao dịch không liên quan đến claim. Agent chọn **giao dịch mục tiêu** từ evidence theo topic: ngày giao trễ đã được xác nhận (claim giao hàng), trạng thái canceled/unavailable, ngày có `reconciliation_mismatch`, ngày có nhiều capture, mốc refund pending/failed, hoặc với `unsupported_claim` là giao dịch không có dấu hiệu lỗi nào. Shipment-agent phân tích timeline của giao dịch đó (hạn bàn giao seller gần nhất sau ngày mua, sự kiện giao trễ cùng ngày giao); đơn chưa giao không bị gán seller trễ. Payment-agent tính captured/refunded/refundable trên capture của giao dịch mục tiêu. Conflict-agent ghi các field lệch giữa `get_order` và customer history, nguồn được chọn và mã giải quyết; confidence chỉ bị hạ khi còn xung đột chưa giải quyết.

## Quyết định và kiểm chứng

Policy-agent xác nhận topic của claim khi evidence ủng hộ, ngược lại kết luận `unsupported_claim`; trạng thái, hành động, số tiền và loại bên chịu trách nhiệm lấy từ rule của policy, còn danh tính seller lấy từ evidence của case. Confidence phản ánh mức kiểm chứng (0.9 khi topic được evidence xác nhận, 0.6 khi claim cụ thể không được xác nhận, 0.35 khi thiếu evidence). Finalizer giới hạn tiền hoàn bởi phần đã thanh toán chưa hoàn và kiểm tra tổng refund lines.

Claim verdicts và output refs là tập con của ledger. Verifier kiểm tra schema L3B V2, đúng case ID, entity không vừa resolved vừa rejected, late seller nằm trong affected sellers, và các bất biến tài chính. Quyền sở hữu ref theo team/run/case cần feedback từ server; validator local chỉ chứng minh ref đã thấy trong phiên chạy.

## Kiểm thử

`pytest -q` gồm test cho A2A, định tuyến tool, bỏ qua candidate giả khi order đã được kiểm chứng, kế hoạch tool theo topic, chọn giao dịch mục tiêu cho `unsupported_claim` và đơn hủy, và lỗi kết nối không bị ghi thành "thiếu evidence". Lệnh kiểm tra: `day09 validate-inputs`, `day09 mcp-tools`, `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`.
