# 🌑 Dark Gym V2

Bản nâng cấp của hệ thống Lunar Eclipse Tower.

## Owner

Owner ID duy nhất của hệ thống: `1031799680792809522`

Các lệnh quản lý Gym không còn yêu cầu quyền Administrator của Discord server.

## Tính năng V2

- 3 tầng Lunar Eclipse Tower.
- Promote Quản Tháp bằng Owner ID.
- Challenge mở trong 12 giờ.
- Chặn mở challenge trùng hoặc khi Gym đang bảo hộ/trận đang diễn ra.
- Random Challenger và tạo Active Match có Match ID.
- Chốt kết quả bằng Active Match, không cần nhập tay người thách đấu.
- 48 giờ revenge cooldown khi thua.
- 12 giờ bảo hộ khi chiếm Gym.
- +15 Dark Stardust cho Quản Tháp phòng thủ thành công.
- +10 Dark Stardust mỗi 3 giờ cho mỗi tầng đang có Quản Tháp.
- `/dark_profile` — hồ sơ Trainer.
- `/dark_rank` — leaderboard Wins / Captures / Defenses.
- `/lich_su` — lịch sử 100 trận gần nhất.
- `/tran_dang_dien_ra` — các trận đang chờ chốt.
- Defense Streak cho từng Gym.
- Achievement: First Blood, Conqueror, Guardian, Dark Master, Unstoppable, Eclipse.
- Lưu dữ liệu bằng PostgreSQL (bảng `dark_gym_state`), không reset khi restart hoặc deploy.
- Tự bổ sung các trường schema mới mà không làm mất dữ liệu cũ.

## Command chính

### Người chơi
- `/thap_gym`
- `/thach_dau tang:<1-3>`
- `/danh_sach_cho tang:<1-3>`
- `/tran_dang_dien_ra`
- `/dark_profile [member]`
- `/dark_rank`
- `/lich_su [member]`
- `/stardust_altar`
- `/teambuilding`

### Owner
- `/promote_darkgym member tang`
- `/mo_thach_dau tang khung_gio_quan_thap khung_gio_nguoi_dau`
- `/huy_thach_dau tang`
- `/ket_qua_gym tang ket_qua`

## Lưu ý

- Bot cần được cấp quyền gửi message/embed trong `GYM_CHANNEL_ID`.
- Biến môi trường bắt buộc:
  - `DISCORD_TOKEN`: token của bot.
  - `DATABASE_URL`: chuỗi kết nối PostgreSQL (bắt buộc TLS).
- Biến tùy chọn: `PORT` (mặc định `8080`, dùng cho health check của Render).
- File `gym_data.json` cũ không còn được dùng và bị bỏ qua.
