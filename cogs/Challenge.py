import asyncio
import logging
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import gym_logic as gl
from cogs.core import GYM_CHANNEL_ID, is_gym_channel, is_owner, read_data, update_data

log = logging.getLogger(__name__)

TierOption = app_commands.Range[int, 1, 3]

# --- Chống spam và giới hạn tần suất gọi API Discord ----------------------- #

CLICK_COOLDOWN = 3.0        # giây tối thiểu giữa hai lần bấm nút của một người
COUNT_REFRESH_DELAY = 5.0   # gộp nhiều lượt bấm thành một lần sửa embed

_last_click = {}
_pending_refresh = {}


def _click_allowed(user_id):
    """Chặn bấm nút liên tục: mỗi lượt bấm là một transaction + một lần gọi API."""
    moment = time.monotonic()
    if len(_last_click) > 5000:
        for uid, at in list(_last_click.items()):
            if moment - at > 60:
                _last_click.pop(uid, None)
    last = _last_click.get(user_id)
    if last is not None and moment - last < CLICK_COOLDOWN:
        return False
    _last_click[user_id] = moment
    return True


def _schedule_count_refresh(tier, message, message_id):
    """Cập nhật số ứng viên trên thông báo, gộp các lượt bấm trong 5 giây.

    Sửa embed ngay mỗi lượt bấm sẽ đụng giới hạn ~5 lần sửa/5 giây cho một tin
    nhắn khi nhiều người bấm cùng lúc. Vì vậy chỉ hẹn một lần sửa duy nhất.
    """
    task = _pending_refresh.get(tier)
    if task is not None and not task.done():
        return

    async def refresh():
        try:
            await asyncio.sleep(COUNT_REFRESH_DELAY)
            data = await read_data(fresh=True)
            session = data["active_registrations"].get(tier)
            if not session or session.get("announcement_message_id") != message_id:
                return
            await message.edit(
                embed=build_announcement_embed(
                    tier, data["towers"][tier].get("title", "Dark Gym"), session
                )
            )
        except (discord.HTTPException, KeyError, TypeError):
            log.exception("Không cập nhật được số ứng viên Tầng %s", tier)
        finally:
            _pending_refresh.pop(tier, None)

    _pending_refresh[tier] = asyncio.create_task(refresh())


WINDOW_ERRORS = {
    "past": "Giờ bắt đầu phải ở tương lai trong ngày mai.",
    "order": "Giờ kết thúc phải sau giờ bắt đầu trong ngày mai.",
    "range": "Khung giờ thách đấu chỉ được nằm trong khoảng 07:00–23:00 (giờ Việt Nam).",
}


# --------------------------------------------------------------------------- #
# Tiện ích
# --------------------------------------------------------------------------- #

async def get_channel(client, channel_id):
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


async def delete_message_quietly(client, channel_id, message_id):
    """Xóa tin nhắn cũ nếu còn; mọi lỗi đều bỏ qua."""
    if not message_id:
        return
    channel = await get_channel(client, int(channel_id or GYM_CHANNEL_ID))
    if channel is None:
        return
    try:
        await channel.get_partial_message(int(message_id)).delete()
    except (discord.HTTPException, AttributeError):
        pass


async def reply_error(interaction, error, where):
    log.error("Lỗi trong %s", where, exc_info=error)
    text = "Có lỗi xảy ra khi xử lý yêu cầu. Hãy thử lại sau."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


def build_announcement_embed(tier, title, session):
    start, end = gl.parse_dt(session["start_at"]), gl.parse_dt(session["end_at"])
    embed = discord.Embed(
        title=f"THÁCH ĐẤU TẦNG {tier} — {title}",
        description="Quản Tháp đã xác nhận lịch rảnh. Bấm nút bên dưới để đăng ký hoặc hủy. Đến giờ bắt đầu, bot chọn ngẫu nhiên đúng 1 người.",
        color=0x2ecc71,
    )
    embed.add_field(name="Quản Tháp", value=f"<@{session['owner_id']}>", inline=False)
    embed.add_field(name="Khung giờ rảnh (giờ Việt Nam)", value=f"{start:%d/%m/%Y %H:%M} – {end:%d/%m/%Y %H:%M}", inline=False)
    embed.add_field(name="Bắt đầu", value=f"<t:{int(start.timestamp())}:F> (<t:{int(start.timestamp())}:R>)", inline=False)
    embed.add_field(name="Ứng viên hiện tại", value=str(len(session.get("challengers", []))), inline=False)
    embed.add_field(name="Teambuilding", value=gl.get_teambuilding_text(), inline=False)
    return embed


async def post_announcement(channel, tier, title, session, silent=False):
    # silent=True: tin nhắc không đẩy thông báo cho cả server đông người.
    return await channel.send(
        content=gl.announcement_text(tier, session),
        embed=build_announcement_embed(tier, title, session),
        view=RegistrationView(tier),
        silent=silent,
    )


# --------------------------------------------------------------------------- #
# Modal / View
# --------------------------------------------------------------------------- #

class AvailabilityModal(discord.ui.Modal, title="Nhập lịch rảnh của Quản Tháp"):
    start = discord.ui.TextInput(label="Giờ bắt đầu (HH:MM)", placeholder="20:00", max_length=5)
    end = discord.ui.TextInput(label="Giờ kết thúc (HH:MM)", placeholder="22:00", max_length=5)

    def __init__(self, tier):
        super().__init__(timeout=300)
        self.tier = str(tier)

    async def on_submit(self, interaction):
        # Discord chỉ cho 3 giây để phản hồi; phần còn lại chạy sau khi đã defer.
        await interaction.response.defer(ephemeral=True)
        tier = self.tier
        current = gl.now()
        tomorrow = (current + timedelta(days=1)).date()
        try:
            start = gl.parse_hhmm(str(self.start.value), tomorrow)
            end = gl.parse_hhmm(str(self.end.value), tomorrow)
        except ValueError:
            return await interaction.followup.send("Sai định dạng. Chỉ nhập giờ theo HH:MM, ví dụ 20:00.", ephemeral=True)
        problem = gl.validate_window(start, end, current)
        if problem:
            return await interaction.followup.send(WINDOW_ERRORS[problem], ephemeral=True)

        user_id = interaction.user.id
        status, payload = await update_data(
            lambda d: gl.submit_schedule(d, tier, user_id, start, end, current, channel_id=GYM_CHANNEL_ID)
        )
        errors = {
            "not_owner": "Chỉ Quản Tháp hiện tại mới được nhập lịch.",
            "match": "Tầng này đang có trận chưa chốt kết quả.",
            "has_challengers": "Đã có người đăng ký theo lịch hiện tại nên không thể đổi. Hãy nhờ Owner dùng /huy_thach_dau để mở lại.",
            "overlap": f"Lịch bị trùng với Tầng {payload}. Hãy chọn khung giờ khác.",
        }
        if status != "ok":
            return await interaction.followup.send(errors[status], ephemeral=True)

        session = payload["session"]
        note = ""
        channel = await get_channel(interaction.client, GYM_CHANNEL_ID)
        if channel is None:
            note = "\nLưu ý: bot không tìm thấy kênh Gym nên chưa đăng được thông báo."
        else:
            await delete_message_quietly(interaction.client, GYM_CHANNEL_ID, payload["old_message_id"])
            try:
                message = await post_announcement(channel, tier, payload["title"], session)
            except discord.HTTPException:
                log.exception("Không đăng được thông báo thách đấu Tầng %s", tier)
                note = "\nLưu ý: bot không đăng được thông báo vào kênh Gym (thiếu quyền?)."
            else:
                await update_data(
                    lambda d: gl.mark_announced(d, tier, session["start_at"], gl.now(), message.id, reschedule=False)
                )

        await interaction.followup.send(
            f"Đã ghi nhận lịch Tầng {tier} cho ngày mai ({start:%d/%m/%Y}): {start:%H:%M}–{end:%H:%M} (giờ Việt Nam). "
            f"Bot sẽ nhắc lại mỗi 30 phút đến giờ bắt đầu.{note}",
            ephemeral=True,
        )

    async def on_error(self, interaction, error):
        await reply_error(interaction, error, "AvailabilityModal")


class AvailabilityView(discord.ui.View):
    def __init__(self, tier):
        super().__init__(timeout=None)
        self.tier = str(tier)
        button = discord.ui.Button(label="Nhập lịch rảnh", style=discord.ButtonStyle.primary, custom_id=f"challenge:availability:{tier}")
        button.callback = self.callback_open
        self.add_item(button)

    async def callback_open(self, interaction):
        # Mở form ngay (phải phản hồi trong 3 giây). Quyền Quản Tháp được kiểm tra khi gửi form.
        await interaction.response.send_modal(AvailabilityModal(self.tier))

    async def on_error(self, interaction, error, item):
        await reply_error(interaction, error, "AvailabilityView")


class RegistrationView(discord.ui.View):
    def __init__(self, tier):
        super().__init__(timeout=None)
        self.tier = str(tier)
        button = discord.ui.Button(label="Đăng ký / Hủy đăng ký", style=discord.ButtonStyle.success, custom_id=f"challenge:register:{tier}")
        button.callback = self.toggle
        self.add_item(button)

    async def toggle(self, interaction):
        tier, user_id, current = self.tier, interaction.user.id, gl.now()
        if not _click_allowed(user_id):
            return await interaction.response.send_message(
                "Bạn bấm hơi nhanh, vui lòng thử lại sau vài giây.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        status, payload = await update_data(lambda d: gl.toggle_registration(d, tier, user_id, current))

        if status == "closed":
            text = "Phiên đăng ký đã đóng."
        elif status == "owner":
            text = "Quản Tháp không thể đăng ký thách đấu tầng mình quản lý."
        elif status == "locked":
            text = "Đã đến giờ bắt đầu, đăng ký đã khóa."
        elif status == "cooldown":
            text = f"Bạn vừa thua ở Tầng {tier} nên phải chờ thêm {gl.format_remaining(payload)} mới được thách đấu lại."
        else:
            text = "Bạn đã hủy đăng ký." if status == "left" else f"Đã ghi danh Tầng {tier}. Hiện có {payload['count']} ứng viên."
            # Cập nhật số ứng viên trên chính tin nhắn có nút bấm (gộp 5 giây/lần).
            if interaction.message is not None:
                _schedule_count_refresh(tier, interaction.message, interaction.message.id)
        await interaction.followup.send(text, ephemeral=True)

    async def on_error(self, interaction, error, item):
        await reply_error(interaction, error, "RegistrationView")


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #

class Challenge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        for tier in ("1", "2", "3"):
            self.bot.add_view(AvailabilityView(tier))
            self.bot.add_view(RegistrationView(tier))
        self.check_challenge_cloisters.start()

    def cog_unload(self):
        self.check_challenge_cloisters.cancel()

    @tasks.loop(minutes=1)
    async def check_challenge_cloisters(self):
        # Một lỗi (DB, Discord) không được phép làm vòng lặp dừng hẳn.
        try:
            snapshot = await read_data()
            current = gl.now()
            for tier, session in list(snapshot["active_registrations"].items()):
                try:
                    try:
                        due = current >= gl.parse_dt(session["start_at"])
                    except (KeyError, TypeError, ValueError):
                        due = True  # phiên hỏng: resolve_session sẽ xóa
                    if due:
                        await self._start_match(tier, current)
                    else:
                        await self._maybe_remind(tier, session, snapshot, current)
                except Exception:
                    log.exception("Lỗi khi xử lý phiên thách đấu Tầng %s", tier)
        except Exception:
            log.exception("Lỗi trong vòng kiểm tra thách đấu")

    @check_challenge_cloisters.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    async def _start_match(self, tier, current):
        """Đến giờ: bốc thăm (nguyên tử trong DB) rồi mới thông báo."""
        result = await update_data(lambda d: gl.resolve_session(d, tier, current))
        if result is None:
            return
        status, payload = result
        if status == "invalid":
            return
        channel_id = payload["channel_id"] if status == "match" else payload
        channel = await get_channel(self.bot, int(channel_id or GYM_CHANNEL_ID))
        if channel is None:
            return
        try:
            if status == "empty":
                await channel.send(f"Lịch thách đấu Tầng {tier} đã đến giờ nhưng không có người đăng ký. Phiên đã đóng.", silent=True)
            elif status == "cancelled":
                await channel.send(f"Phiên thách đấu Tầng {tier} bị hủy vì không có Quản Tháp hoặc đã có trận đang diễn ra.", silent=True)
            else:
                start, end = payload["start"], payload["end"]
                embed = discord.Embed(
                    title=f"MATCH #{payload['match_id']} — BỐC THĂM TẦNG {tier}",
                    description=f"Đã chọn ngẫu nhiên 1/{payload['count']} ứng viên.",
                    color=0xe74c3c,
                )
                embed.add_field(name="Quản Tháp", value=f"<@{payload['owner_id']}>", inline=True)
                embed.add_field(name="Người thách đấu", value=f"<@{payload['selected']}>", inline=True)
                embed.add_field(name="Khung giờ đã xác nhận", value=f"{start:%d/%m/%Y %H:%M} – {end:%d/%m/%Y %H:%M} giờ Việt Nam", inline=False)
                embed.add_field(name="Teambuilding", value=gl.get_teambuilding_text(), inline=False)
                await channel.send(content="Đã chọn xong người thách đấu ngẫu nhiên. Vui lòng xem thông tin trong thông báo bên dưới.", embed=embed)
        except discord.HTTPException:
            # Kết quả đã được lưu; chỉ riêng việc thông báo thất bại.
            log.exception("Không gửi được thông báo kết quả bốc thăm Tầng %s", tier)

    async def _maybe_remind(self, tier, session, snapshot, current):
        """Nhắc lại mỗi 30 phút bằng cách SỬA tin thông báo cũ.

        Trước đây bot xóa rồi đăng lại, tức là mỗi 30 phút lại đẩy một tin mới
        vào kênh (3 tầng = 6 tin/giờ). Trên server đông người đó vừa gây nhiễu
        vừa tốn lượt gọi API. Sửa tại chỗ chỉ tốn một lần gọi và không làm trôi
        tin nhắn.
        """
        raw = session.get("next_announcement_at")
        try:
            next_at = gl.parse_dt(raw) if raw else current + gl.ANNOUNCE_INTERVAL
        except (TypeError, ValueError):
            next_at = current
        if current < next_at:
            return

        channel_id = int(session.get("channel_id") or GYM_CHANNEL_ID)
        channel = await get_channel(self.bot, channel_id)
        title = snapshot["towers"][tier].get("title", "Dark Gym")
        message = None

        stored_id = session.get("announcement_message_id")
        if channel is not None and stored_id:
            try:
                partial = channel.get_partial_message(int(stored_id))
                await partial.edit(
                    content=gl.announcement_text(tier, session),
                    embed=build_announcement_embed(tier, title, session),
                    view=RegistrationView(tier),
                )
                message = partial
            except discord.NotFound:
                message = None  # tin đã bị xóa tay -> đăng lại bên dưới
            except discord.HTTPException:
                log.exception("Không sửa được thông báo nhắc Tầng %s", tier)

        if message is None and channel is not None:
            try:
                message = await post_announcement(channel, tier, title, session, silent=True)
            except discord.HTTPException:
                log.exception("Không đăng được thông báo nhắc Tầng %s", tier)

        message_id = message.id if message else None
        await update_data(lambda d: gl.mark_announced(d, tier, session["start_at"], gl.now(), message_id))

    async def owner_only(self, interaction):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("Chỉ Owner của Dark Gym mới có quyền dùng lệnh này.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="mo_thach_dau", description="[OWNER] Yêu cầu Quản Tháp nhập lịch rảnh.")
    @is_gym_channel()
    async def mo_thach_dau(self, interaction: discord.Interaction, tang: TierOption):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        tier, current = str(tang), gl.now()
        status, payload = await update_data(lambda d: gl.request_challenge(d, tier, interaction.user.id, current))

        if status == "no_owner":
            return await interaction.followup.send("Tầng này chưa có Quản Tháp.", ephemeral=True)
        if status == "busy":
            return await interaction.followup.send("Tầng này đã có lịch đăng ký hoặc trận chưa chốt.", ephemeral=True)
        if status == "protected":
            return await interaction.followup.send(f"Tầng đang được bảo hộ thêm {gl.format_remaining(payload)}.", ephemeral=True)

        manager_id = int(payload["user_id"])
        try:
            manager = self.bot.get_user(manager_id) or await self.bot.fetch_user(manager_id)
            await manager.send(
                f"Owner yêu cầu bạn nhập lịch rảnh cho Tầng {tier} — {payload['title']}. "
                "Lịch mặc định là ngày mai; chỉ cần nhập giờ theo HH:MM (giờ Việt Nam).",
                view=AvailabilityView(tier),
            )
        except (discord.Forbidden, discord.HTTPException):
            # Không DM được thì không để lại yêu cầu "treo".
            await update_data(lambda d: gl.cancel_request(d, tier))
            return await interaction.followup.send("Không thể DM Quản Tháp. Hãy bật tin nhắn riêng rồi chạy lệnh lại.", ephemeral=True)
        await interaction.followup.send(f"Đã gửi yêu cầu lịch rảnh cho Quản Tháp Tầng {tier}.", ephemeral=True)

    @app_commands.command(name="huy_thach_dau", description="[OWNER] Hủy yêu cầu hoặc lịch thách đấu.")
    @is_gym_channel()
    async def huy_thach_dau(self, interaction: discord.Interaction, tang: TierOption):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        tier = str(tang)
        removed, message_id, channel_id = await update_data(lambda d: gl.cancel_challenge(d, tier))
        if not removed:
            return await interaction.followup.send("Tầng này không có yêu cầu/lịch thách đấu.", ephemeral=True)
        await delete_message_quietly(self.bot, channel_id, message_id)
        await interaction.followup.send(f"Đã hủy lịch/yêu cầu Tầng {tier}.", ephemeral=True)

    @app_commands.command(name="thach_dau", description="Kiểm tra trạng thái đăng ký thách đấu.")
    @is_gym_channel()
    async def thach_dau(self, interaction: discord.Interaction, tang: TierOption):
        await interaction.response.defer(ephemeral=True)
        session = (await read_data())["active_registrations"].get(str(tang))
        if not session:
            return await interaction.followup.send("Tầng hiện không mở đăng ký.", ephemeral=True)
        text = "Bạn đã ghi danh; dùng nút trên thông báo để hủy." if interaction.user.id in session.get("challengers", []) else "Hãy dùng nút Đăng ký / Hủy đăng ký trong thông báo công khai."
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="danh_sach_cho", description="Xem danh sách ứng viên đã đăng ký.")
    @is_gym_channel()
    async def danh_sach_cho(self, interaction: discord.Interaction, tang: TierOption):
        await interaction.response.defer(ephemeral=True)
        tier = str(tang)
        session = (await read_data())["active_registrations"].get(tier)
        if not session:
            return await interaction.followup.send(f"Tầng {tang} hiện không mở đăng ký.", ephemeral=True)
        start = gl.parse_dt(session["start_at"])
        people = session.get("challengers", [])
        embed = discord.Embed(
            title=f"DANH SÁCH ĐĂNG KÝ TẦNG {tier}",
            description="\n".join(f"• <@{uid}>" for uid in people) if people else "Chưa có ứng viên.",
            color=0x3498db,
        )
        embed.add_field(name="Giờ bắt đầu", value=f"{start:%d/%m/%Y %H:%M} giờ Việt Nam")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="tran_dang_dien_ra", description="Xem các trận Dark Gym đang chờ chốt kết quả.")
    @is_gym_channel()
    async def tran_dang_dien_ra(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        matches = (await read_data())["active_matches"]
        embed = discord.Embed(title="ACTIVE DARK GYM MATCHES", color=0xe74c3c)
        if not matches:
            embed.description = "Không có trận nào đang diễn ra."
        for tier, match in sorted(matches.items()):
            embed.add_field(name=f"Match #{match['match_id']} — Tầng {tier}", value=f"<@{match['owner_id']}> vs <@{match['challenger_id']}>", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Challenge(bot))
