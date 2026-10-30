# 🏟️ Weekly Gym League

Bot quản lý **một** Gym: mỗi tuần tìm ra người thách đấu Gym Leader, ai thắng thì
giữ chức cho tuần sau. Không có tiền tệ, không có huy hiệu, không cần quyền
Administrator.

Top 1/2/3 và 3 danh hiệu do người quản lý Gym trao thưởng **ngoài Discord**;
bot chỉ ghi nhận kết quả và lịch sử trận.

## Chu kỳ một tuần (giờ Việt Nam)

| Thời điểm | Việc xảy ra |
|---|---|
| Thứ 2 00:00 | Mở đăng ký **cả tuần**: tin `Đăng kí thách đấu Gym Leader - WL<n>` (im lặng) + nút đăng ký |
| Cách 8 giờ | Bot đăng lại panel đăng ký (im lặng); bản cũ mất nút |
| Chủ Nhật 00:00 | Bốc thăm (tin nhắn im lặng) |
| Chủ Nhật 06:00 | Thông báo lại tình trạng tuần (im lặng) |
| Chủ Nhật 18:00 | Ping nhắc một lần (có thông báo) |
| Chủ Nhật 23:59:59 | Hạn chót nhận kết quả |
| Thứ 2 00:00 tuần sau | Người thắng trận tranh chức **tự động nhậm chức Gym Leader** |

Số `WL` tăng mỗi tuần (WL1, WL2, WL3, …) và chỉ reset khi chạy `/reset`.

Kết quả bốc thăm:

- **0 người** → Gym Leader giữ chức, nhiệm kỳ được gia hạn thêm 1 tuần.
- **1 người** → người đó thách đấu Gym Leader ngay, **BO3**.
- **2+ người** → chọn ngẫu nhiên 2 người đánh **BO1**; người thắng thách đấu
  Gym Leader trong **BO3**.
- Chưa có Gym Leader → người thắng (hoặc người đăng ký duy nhất) nhậm chức
  vào **00:00 thứ 2** kế tiếp.

Hai người chơi **tự thương lượng thời gian thi đấu** sau khi có kết quả bốc thăm.
Quá hạn Chủ Nhật mà chưa có kết quả → Gym Leader giữ chức và được gia hạn.

Người thắng BO3 **không đổi ngay** trong Chủ Nhật: bot ghi nhận là người kế nhiệm
và trao chức đúng **00:00 thứ 2**, để nhiệm kỳ luôn tròn tuần.

### Chat riêng của từng trận

Ngay sau khi bốc thăm, bot tạo một **private thread** (chỉ 2 người đấu + owner thấy):

1. Hai người đánh xong, gửi **ảnh chụp màn hình** kết quả vào thread.
2. Một người bấm **📤 Nộp kết quả** → nhập tỷ số theo góc nhìn của mình (`2-1` = tôi thắng 2-1).
   Bot tự tìm ảnh mới nhất của người đó và ghi biên bản.
3. Người còn lại bấm **✅ Xác nhận** (hoặc **❌ Không đúng**).

Xác nhận xong → bot đăng kết quả + **ảnh minh chứng** ra kênh chính rồi **xóa thread**.
Nếu bị báo sai, bot ping owner; owner dùng `/ket_qua` để chốt hoặc bấm ✅ để chấp nhận biên bản.

Tỷ số hợp lệ: BO1 `1-0 / 0-1`, BO3 `2-0 / 2-1 / 0-2 / 1-2`.

### Ưu tiên người chưa được chọn

Người đăng ký mà không được bốc trúng sẽ được **+1 ưu tiên** cho tuần sau, nên
đăng ký nhiều tuần sẽ có cơ hội đánh cao hơn. Ai được chọn thì ưu tiên về 0.

## Lệnh

### Người chơi
- `/gym` — Gym Leader hiện tại và tình trạng tuần này.
- `/dang_ky` — đăng ký / hủy đăng ký (dùng thay cho nút).
- `/lich_su [member]` — 10 trận gần nhất.
- `/bang_xep_hang` — top thắng nhiều và top số lần làm Leader.

### Owner (ID trong `cogs/core.py`)
- `/ket_qua nguoi_thang:@X` — chốt người thắng trận đang diễn ra (BO1 hoặc BO3).
- `/gan_leader member:@X` — gán Gym Leader thủ công cho tuần sau.
- `/huy_tuan` — hủy tuần hiện tại, gia hạn Leader.
- `/tao_dang_ky` — đăng lại tin đăng ký ngay (khi bot tắt đúng lúc mở tuần, hoặc muốn đẩy tin lên).
- `/reset` — **xóa toàn bộ dữ liệu bot**, xác nhận 2 lần (dùng để test). Sau khi reset, tuần mới WL1 tự mở trong ít phút.

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

## Cấu trúc

- `match_logic.py` — toàn bộ luật tuần + nộp/xác nhận kết quả (không import discord/psycopg, test được).
- `cogs/core.py` — pool Postgres, cache đọc, helper dùng chung.
- `cogs/League.py` — cog Discord: vòng lặp 2 phút, nút đăng ký, chat riêng, slash commands.
- `tests/` — test luật tuần, nộp/xác nhận kết quả và các chốt chặn tải.

## Kiểm thử

```bash
python -m pytest tests -q
```
