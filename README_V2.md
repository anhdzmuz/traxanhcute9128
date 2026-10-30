# 🏟️ Weekly Gym League

Bot quản lý **một** Gym: mỗi tuần tìm ra người thách đấu Gym Leader, ai thắng thì
giữ chức cho tuần sau. Không có tiền tệ, không có huy hiệu, không cần quyền
Administrator.

Top 1/2/3 và 3 danh hiệu do người quản lý Gym trao thưởng **ngoài Discord**;
bot chỉ ghi nhận kết quả và lịch sử trận.

## Chu kỳ một tuần (giờ Việt Nam)

| Thời điểm | Việc xảy ra |
|---|---|
| Thứ 2 00:00 | Mở đăng ký **cả tuần** + nút "Đăng ký / Hủy đăng ký" (tin nhắn im lặng) |
| Chủ Nhật 00:00 | Bốc thăm (tin nhắn im lặng) |
| Chủ Nhật 06:00 | Thông báo lại tình trạng tuần (im lặng) |
| Chủ Nhật 18:00 | Ping nhắc một lần (có thông báo) |
| Chủ Nhật 23:59:59 | Hạn chót nhận kết quả |

Kết quả bốc thăm:

- **0 người** → Gym Leader giữ chức, nhiệm kỳ được gia hạn thêm 1 tuần.
- **1 người** → người đó thách đấu Gym Leader ngay, **BO3**.
- **2+ người** → chọn ngẫu nhiên 2 người đánh **BO1**; người thắng thách đấu
  Gym Leader trong **BO3**.
- Chưa có Gym Leader → người thắng (hoặc người đăng ký duy nhất) nhận chức luôn.

Hai người chơi **tự thương lượng thời gian thi đấu** sau khi có kết quả bốc thăm.
Quá hạn Chủ Nhật mà chưa có kết quả → Gym Leader giữ chức và được gia hạn.

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

## Cấu hình

| Biến | Ý nghĩa |
|---|---|
| `DISCORD_TOKEN` | Token bot. |
| `DATABASE_URL` | Chuỗi kết nối Postgres (Neon). |

`OWNER_ID` và `GYM_CHANNEL_ID` nằm trong `cogs/core.py`.

## Cấu trúc

- `match_logic.py` — toàn bộ luật tuần (không import discord/psycopg, test được).
- `cogs/core.py` — pool Postgres, cache đọc, helper dùng chung.
- `cogs/League.py` — cog Discord: vòng lặp 2 phút, nút đăng ký, slash commands.
- `tests/` — test luật tuần và các chốt chặn tải.

## Kiểm thử

```bash
python -m pytest tests -q
```
