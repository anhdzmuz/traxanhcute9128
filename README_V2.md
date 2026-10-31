# 🏟️ Binh Đoàn Bóng Tối ┋ Gym Dark TGA

Bot quản lý **một** Gym: mỗi tuần chạy một giải để tìm ra Gym Leader của tuần sau.
Không có tiền tệ, không có huy hiệu, không cần quyền Administrator.

Top 1/2/3, danh hiệu và Pokémon Shiny thưởng do người điều hành Gym trao
**ngoài Discord**; bot chỉ lo thể thức, chat riêng từng trận và lịch sử kết quả.

## Thể thức (giờ Việt Nam)

Mỗi giải chạy trọn **1 tuần**; đăng ký của tuần trước là danh sách của tuần này.

| Thời điểm | Việc xảy ra |
|---|---|
| Thứ 2 00:00 | Chốt đăng ký tuần trước → mở giải. Panel mới `Đăng kí thách đấu Gym Leader - WL<n>` được đăng (im lặng) |
| Cách 8 giờ | Bot đăng lại panel đăng ký (im lặng); bản cũ mất nút |
| Thứ 2 – Thứ 4 | **Vòng loại BO1**: bot ghép cặp ngẫu nhiên, mỗi người đấu 1 trận; ai thắng (hoặc lẻ cặp được đi tiếp) vào danh sách |
| **00:00 Thứ 5** | Hết hạn vòng loại — cặp nào **chưa báo kết quả thì loại cả 2** |
| Thứ 5 – Thứ 7 | **Ghép cặp lần 2 (BO1)** giữa những người còn lại để chốt danh sách random |
| **00:00 Chủ Nhật** | Bot random **2 Trainer** trong danh sách vào chung kết (sớm hơn nếu mọi trận đã xong); ai vào vòng này mà không được chọn → **+1 ưu tiên** lần sau |
| Chủ Nhật | **Chung kết BO3**: 2 Trainer đấu để giành slot Gym Leader tuần sau |
| Thứ 2 00:00 tuần sau | Người thắng **tự động nhậm chức Gym Leader** |

Số `WL` tăng mỗi tuần (WL1, WL2, WL3, …) và chỉ reset khi chạy `/reset`.

Các trường hợp đặc biệt:

- **0 người** đăng ký → Gym Leader giữ chức, nhiệm kỳ gia hạn 1 tuần.
- **1 người** đăng ký → người đó nhậm chức vào **00:00 Thứ 2** (không cần đánh).
- **2 người** đăng ký → bỏ vòng loại, vào thẳng **chung kết BO3**.
- Vòng loại / ghép cặp lần 2 mà còn **dưới 2 người** sau khi loại → hủy giải, Leader giữ chức.
- Sau vòng loại mà chỉ còn **2 người** → bỏ ghép cặp lần 2, vào thẳng chung kết.
- Chung kết không có kết quả trước hạn → hủy, Leader giữ chức.

## Luật của Gym (bot hiển thị, không tự kiểm tra)

- Tối thiểu **3/6 Pokémon hệ Dark** trong team.
- Chỉ dùng **1 team cố định** trong tuần đấu đó, không đổi giữa các vòng.
- **Hybrid team sheet:** để lộ **2/6** Pokémon.
- Cơ chế cấm chọn: *Updating…*
- Lệ phí: **miễn phí** (tạm thời).
- Lần đầu thành Gym Leader (thời vụ): chọn **1 Pokémon Shiny** trong kho của Gym.

Người chơi xem đầy đủ bằng `/luat`; panel đăng ký cũng hiện phần team building.

## Chat riêng của từng trận

Mỗi cặp đấu (vòng loại, ghép cặp lần 2, chung kết) có một **private thread**
trong `MATCH_CHANNEL_ID`, chỉ 2 người đấu + owner thấy:

1. Hai người tự hẹn giờ trong thread.
2. Đánh xong, gửi **ảnh chụp màn hình** kết quả.
3. Một người bấm **📤 Nộp kết quả** → nhập tỷ số theo góc nhìn của mình (`2-1` = tôi thắng 2-1).
   Bot tự lấy ảnh mới nhất của người đó làm minh chứng.
4. Người còn lại bấm **✅ Xác nhận** (hoặc **❌ Không đúng**).

Xác nhận xong → bot đăng kết quả + **upload lại ảnh minh chứng** ra kênh chính,
rồi **xóa thread**. Nếu bị báo sai, bot ping owner; owner dùng `/ket_qua` để chốt
hoặc bấm ✅ để chấp nhận biên bản.

Tỷ số hợp lệ: BO1 `1-0 / 0-1`, BO3 `2-0 / 2-1 / 0-2 / 1-2`.

## Ưu tiên

Ai vào được vòng **Thứ 5 – Thứ 7** mà không được random chọn vào chung kết sẽ được
**+1 ưu tiên** cho lần sau (ưu tiên càng cao, trọng số random càng lớn).
Người được chọn thì ưu tiên về 0.

## Lệnh

### Người chơi
- `/luat` — toàn bộ luật thi đấu của Gym.
- `/gym` — Gym Leader hiện tại, đăng ký đang mở và trạng thái giải.
- `/dang_ky` — đăng ký / hủy đăng ký (dùng thay cho nút).
- `/lich_su [member]` — 10 trận gần nhất.
- `/bang_xep_hang` — top thắng nhiều và top số lần làm Leader.

### Owner (ID trong `cogs/core.py`)
- `/bat_dau` — chốt đăng ký và mở giải **ngay** (dùng để test, không cần chờ Thứ 2).
- `/ket_qua nguoi_thang:@X` — chốt tay người thắng của trận đang mở (tranh chấp / thiếu biên bản).
- `/gan_leader member:@X` — gán Gym Leader thủ công cho tuần sau.
- `/huy_giai` — hủy giải đang chạy, gia hạn Leader.
- `/tao_dang_ky` — đăng lại tin đăng ký ngay (tự mở lại đăng ký nếu đang đóng).
- `/kiem_tra_quyen` — xem quyền **thực tế** bot đang có trong kênh Gym và kênh trận, kèm các channel override đang chặn.
- `/reset` — **xóa toàn bộ dữ liệu bot**, xác nhận 2 lần. Sau khi reset, panel **WL1** tự mở trong ~2 phút.

## Cấu hình

| Biến | Ý nghĩa |
|---|---|
| `DISCORD_TOKEN` | Token bot. |
| `DATABASE_URL` | Chuỗi kết nối Postgres (Neon). |
| `MATCH_CHANNEL_ID` | Kênh chứa chat riêng của từng trận. Bỏ trống → dùng `GYM_CHANNEL_ID`. |

`OWNER_ID` và `GYM_CHANNEL_ID` nằm trong `cogs/core.py`.

## Quyền bot cần

Không cần Administrator. Chỉ trên kênh trận đấu (`MATCH_CHANNEL_ID`):

- `Create Private Threads` — tạo chat riêng.
- `Send Messages in Threads` — gửi panel và thông báo.
- `Manage Threads` — xóa chat riêng sau khi xác nhận.
- `Read Message History` — tìm ảnh minh chứng.
- `Attach Files` — đăng lại ảnh minh chứng ra kênh chính.

Kênh chính chỉ cần `Send Messages` + `Embed Links` (+ `Attach Files` nếu muốn giữ ảnh minh chứng).

> ⚠️ Quyền ở cấp server **không thắng** channel override. Nếu `@everyone` bị **Deny** `Send Messages` trong kênh, phải thêm **Allow** cho role của bot ngay trong `Edit Channel → Permissions`. Dùng `/kiem_tra_quyen` để xem bot đang thiếu gì.

Panel được gửi ở chế độ im lặng (`silent`). Nếu Discord từ chối cờ này, bot tự gửi lại **không im lặng**; nếu kênh chặn **Embed Links**, bot gửi panel dạng **chữ** (vẫn có nút đăng ký) thay vì báo lỗi.

## Cấu trúc

- `match_logic.py` — toàn bộ luật giải + nộp/xác nhận kết quả (không import discord/psycopg, test được).
- `cogs/core.py` — pool Postgres, cache đọc, helper dùng chung.
- `cogs/League.py` — cog Discord: vòng lặp 2 phút, panel đăng ký, chat riêng từng trận, slash commands.
- `tests/` — test luật giải, nộp/xác nhận kết quả và các chốt chặn tải.

## Kiểm thử

```bash
python -m pytest tests -q
```
