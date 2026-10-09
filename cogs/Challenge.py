import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands

from cogs.core import GYM_CHANNEL_ID, get_teambuilding_text, is_gym_channel, is_owner, load_data, save_data, format_remaining

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
INTERVAL = timedelta(minutes=30)


def now():
    return datetime.now(TZ)


def parse_time(value):
    return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=TZ)


def local_time(value):
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


class AvailabilityModal(discord.ui.Modal, title="Nhập lịch rảnh của Quản Tháp"):
    start = discord.ui.TextInput(label="Bắt đầu (DD/MM/YYYY HH:MM)", placeholder="12/10/2026 20:00", max_length=16)
    end = discord.ui.TextInput(label="Kết thúc (DD/MM/YYYY HH:MM)", placeholder="12/10/2026 22:00", max_length=16)

    def __init__(self, tier):
        super().__init__(timeout=300)
        self.tier = str(tier)

    async def on_submit(self, interaction):
        data = load_data()
        tier = self.tier
        if data["towers"][tier].get("user_id") != interaction.user.id:
            return await interaction.response.send_message("Chỉ Quản Tháp hiện tại mới được nhập lịch.", ephemeral=True)
        try:
            start, end = parse_time(str(self.start.value)), parse_time(str(self.end.value))
        except ValueError:
            return await interaction.response.send_message("Sai định dạng. Hãy nhập DD/MM/YYYY HH:MM, ví dụ 12/10/2026 20:00.", ephemeral=True)
        if start <= now() or end <= start:
            return await interaction.response.send_message("Giờ bắt đầu phải ở tương lai và giờ kết thúc phải sau giờ bắt đầu.", ephemeral=True)
        if tier in data.get("active_matches", {}):
            return await interaction.response.send_message("Tầng này đang có trận chưa chốt kết quả.", ephemeral=True)
        for other, session in data.get("active_registrations", {}).items():
            try:
                other_start, other_end = local_time(session["start_at"]), local_time(session["end_at"])
            except (KeyError, ValueError):
                continue
            if start < other_end and end > other_start:
                return await interaction.response.send_message(f"Lịch bị trùng với Tầng {other}. Hãy chọn khung giờ khác.", ephemeral=True)
        session = {
            "owner_id": interaction.user.id,
            "owner_time": f"{start:%d/%m/%Y %H:%M} - {end:%d/%m/%Y %H:%M} giờ Việt Nam",
            "start_at": start.isoformat(), "end_at": end.isoformat(),
            "next_announcement_at": (now() + INTERVAL).isoformat(),
            "channel_id": GYM_CHANNEL_ID, "challengers": [], "announcement_message_id": None,
        }
        data.setdefault("active_registrations", {})[tier] = session
        data.setdefault("pending_challenges", {}).pop(tier, None)
        save_data(data)
        channel = interaction.client.get_channel(GYM_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(GYM_CHANNEL_ID)
            except discord.HTTPException:
                channel = None
        if channel:
            message = await post_announcement(channel, tier, session, data)
            latest = load_data()
            if tier in latest.get("active_registrations", {}):
                latest["active_registrations"][tier]["announcement_message_id"] = message.id
                save_data(latest)
        await interaction.response.send_message(
            f"Đã ghi nhận lịch Tầng {tier}: {start:%d/%m/%Y %H:%M}–{end:%H:%M} (giờ Việt Nam). Bot sẽ nhắc lại mỗi 30 phút đến giờ bắt đầu.",
            ephemeral=True,
        )


class AvailabilityView(discord.ui.View):
    def __init__(self, tier):
        super().__init__(timeout=None)
        self.tier = str(tier)
        button = discord.ui.Button(label="Nhập lịch rảnh", style=discord.ButtonStyle.primary, custom_id=f"challenge:availability:{tier}")
        button.callback = self.callback_open
        self.add_item(button)

    async def callback_open(self, interaction):
        data = load_data()
        if data["towers"][self.tier].get("user_id") != interaction.user.id:
            return await interaction.response.send_message("Nút này chỉ dành cho Quản Tháp hiện tại của tầng này.", ephemeral=True)
        await interaction.response.send_modal(AvailabilityModal(self.tier))


class RegistrationView(discord.ui.View):
    def __init__(self, tier):
        super().__init__(timeout=None)
        self.tier = str(tier)
        button = discord.ui.Button(label="Đăng ký / Hủy đăng ký", style=discord.ButtonStyle.success, custom_id=f"challenge:register:{tier}")
        button.callback = self.toggle
        self.add_item(button)

    async def toggle(self, interaction):
        data = load_data()
        session = data.get("active_registrations", {}).get(self.tier)
        if not session:
            return await interaction.response.send_message("Phiên đăng ký đã đóng.", ephemeral=True)
        if interaction.user.id == session.get("owner_id"):
            return await interaction.response.send_message("Quản Tháp không thể đăng ký thách đấu tầng mình quản lý.", ephemeral=True)
        if now() >= local_time(session["start_at"]):
            return await interaction.response.send_message("Đã đến giờ bắt đầu, đăng ký đã khóa.", ephemeral=True)
        people = session.setdefault("challengers", [])
        if interaction.user.id in people:
            people.remove(interaction.user.id)
            reply = "Bạn đã hủy đăng ký."
        else:
            people.append(interaction.user.id)
            reply = f"Đã ghi danh Tầng {self.tier}. Hiện có {len(people)} ứng viên."
        save_data(data)
        await interaction.response.send_message(reply, ephemeral=True)


async def post_announcement(channel, tier, session, data=None):
    data = data or load_data()
    start, end = local_time(session["start_at"]), local_time(session["end_at"])
    tower = data["towers"][tier]
    embed = discord.Embed(
        title=f"THÁCH ĐẤU TẦNG {tier} — {tower.get('title', 'Dark Gym')}",
        description="Quản Tháp đã xác nhận lịch rảnh. Bấm nút bên dưới để đăng ký hoặc hủy. Đến giờ bắt đầu, bot chọn ngẫu nhiên đúng 1 người.",
        color=0x2ecc71,
    )
    embed.add_field(name="Quản Tháp", value=f"<@{session['owner_id']}>", inline=False)
    embed.add_field(name="Khung giờ rảnh (giờ Việt Nam)", value=f"{start:%d/%m/%Y %H:%M} – {end:%d/%m/%Y %H:%M}", inline=False)
    embed.add_field(name="Bắt đầu", value=f"<t:{int(start.timestamp())}:F> (<t:{int(start.timestamp())}:R>)", inline=False)
    embed.add_field(name="Ứng viên hiện tại", value=str(len(session.get("challengers", []))), inline=False)
    embed.add_field(name="Teambuilding", value=get_teambuilding_text(), inline=False)
    return await channel.send(content=f"Lịch thách đấu mới tại Tầng {tier}! <@{session['owner_id']}>", embed=embed, view=RegistrationView(tier))


class Challenge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_challenge_cloisters.start()

    async def cog_load(self):
        for tier in ("1", "2", "3"):
            self.bot.add_view(AvailabilityView(tier))
            self.bot.add_view(RegistrationView(tier))

    def cog_unload(self):
        self.check_challenge_cloisters.cancel()

    @tasks.loop(minutes=1)
    async def check_challenge_cloisters(self):
        data = load_data()
        changed = False
        current = now()

        for tier, request in list(data.get("pending_challenges", {}).items()):
            if request.get("sent"):
                continue
            owner_id = data["towers"].get(tier, {}).get("user_id")
            if not owner_id:
                continue
            try:
                manager = self.bot.get_user(int(owner_id)) or await self.bot.fetch_user(int(owner_id))
                await manager.send(f"Owner cần bạn nhập lịch rảnh cho Tầng {tier}.", view=AvailabilityView(tier))
                request["sent"] = True
                changed = True
            except (discord.Forbidden, discord.HTTPException):
                pass

        for tier, session in list(data.get("active_registrations", {}).items()):
            try:
                start, end = local_time(session["start_at"]), local_time(session["end_at"])
            except (KeyError, ValueError, TypeError):
                del data["active_registrations"][tier]
                changed = True
                continue

            channel_id = int(session.get("channel_id", GYM_CHANNEL_ID))
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None

            if current < start:
                next_at = local_time(session.get("next_announcement_at", (current + INTERVAL).isoformat()))
                if current >= next_at:
                    if channel:
                        await post_announcement(channel, tier, session, data)
                    session["next_announcement_at"] = (current + INTERVAL).isoformat()
                    changed = True
                continue

            candidates = list(dict.fromkeys(session.get("challengers", [])))
            owner_id = data["towers"][tier].get("user_id")
            if not candidates:
                if channel:
                    await channel.send(f"Lịch thách đấu Tầng {tier} đã đến giờ nhưng không có người đăng ký. Phiên đã đóng.")
                del data["active_registrations"][tier]
                changed = True
                continue
            if not owner_id or tier in data.get("active_matches", {}):
                if channel:
                    await channel.send(f"Phiên thách đấu Tầng {tier} bị hủy vì không có Quản Tháp hoặc đã có trận đang diễn ra.")
                del data["active_registrations"][tier]
                changed = True
                continue

            # Random selection is performed once, then persisted as an active match.
            selected = random.choice(candidates)
            match_id = self._next_match_id(data)
            data["active_matches"][tier] = {
                "match_id": match_id, "tower": int(tier), "owner_id": int(owner_id),
                "challenger_id": int(selected), "created_at": current.isoformat(),
                "owner_time": session.get("owner_time", "Chưa đặt"),
                "challenger_time": f"{start:%d/%m/%Y %H:%M} giờ Việt Nam",
                "scheduled_start": start.isoformat(), "scheduled_end": end.isoformat(),
            }
            del data["active_registrations"][tier]
            changed = True
            if channel:
                embed = discord.Embed(title=f"MATCH #{match_id} — BỐC THĂM TẦNG {tier}", description=f"Đã chọn ngẫu nhiên 1/{len(candidates)} ứng viên.", color=0xe74c3c)
                embed.add_field(name="Quản Tháp", value=f"<@{owner_id}>", inline=True)
                embed.add_field(name="Người thách đấu", value=f"<@{selected}>", inline=True)
                embed.add_field(name="Khung giờ đã xác nhận", value=f"{start:%d/%m/%Y %H:%M} – {end:%d/%m/%Y %H:%M} giờ Việt Nam", inline=False)
                embed.add_field(name="Teambuilding", value=get_teambuilding_text(), inline=False)
                await channel.send(content=f"Chúc mừng <@{selected}>! Bạn được chọn thi đấu với <@{owner_id}>.", embed=embed)

        if changed:
            save_data(data)

    @check_challenge_cloisters.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def _next_match_id(data):
        ids = [int(x.get("match_id", 0)) for x in data.get("match_history", []) if str(x.get("match_id", "")).isdigit()]
        ids += [int(x.get("match_id", 0)) for x in data.get("active_matches", {}).values() if str(x.get("match_id", "")).isdigit()]
        return max(ids + [0]) + 1

    async def owner_only(self, interaction):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("Chỉ Owner của Dark Gym mới có quyền dùng lệnh này.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="mo_thach_dau", description="[OWNER] Yêu cầu Quản Tháp nhập lịch rảnh.")
    @is_gym_channel()
    async def mo_thach_dau(self, interaction: discord.Interaction, tang: int):
        if not await self.owner_only(interaction):
            return
        if tang not in (1, 2, 3):
            return await interaction.response.send_message("Chọn tầng từ 1 đến 3.", ephemeral=True)
        data = load_data()
        tier = str(tang)
        tower = data["towers"][tier]
        if not tower.get("user_id"):
            return await interaction.response.send_message("Tầng này chưa có Quản Tháp.", ephemeral=True)
        if tier in data.get("active_registrations", {}) or tier in data.get("active_matches", {}):
            return await interaction.response.send_message("Tầng này đã có lịch đăng ký hoặc trận chưa chốt.", ephemeral=True)
        if tower.get("protected_until"):
            protection = datetime.fromisoformat(tower["protected_until"])
            if protection.tzinfo is None:
                protection = protection.replace(tzinfo=TZ)
            if protection > now():
                return await interaction.response.send_message(f"Tầng đang được bảo hộ thêm {format_remaining(protection - now())}.", ephemeral=True)
        data.setdefault("pending_challenges", {})[tier] = {"requested_by": interaction.user.id, "requested_at": now().isoformat(), "sent": False}
        save_data(data)
        try:
            manager = self.bot.get_user(int(tower["user_id"])) or await self.bot.fetch_user(int(tower["user_id"]))
            await manager.send(f"Owner yêu cầu bạn nhập lịch rảnh cho Tầng {tier} — {tower.get('title', 'Dark Gym')}. Nhấn nút rồi nhập ngày giờ theo giờ Việt Nam.", view=AvailabilityView(tier))
            data = load_data()
            data["pending_challenges"][tier]["sent"] = True
            save_data(data)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("Không thể DM Quản Tháp. Hãy bật tin nhắn riêng rồi chạy lệnh lại.", ephemeral=True)
        await interaction.response.send_message(f"Đã gửi yêu cầu lịch rảnh cho Quản Tháp Tầng {tier}.", ephemeral=True)

    @app_commands.command(name="huy_thach_dau", description="[OWNER] Hủy yêu cầu hoặc lịch thách đấu.")
    @is_gym_channel()
    async def huy_thach_dau(self, interaction: discord.Interaction, tang: int):
        if not await self.owner_only(interaction):
            return
        if tang not in (1, 2, 3):
            return await interaction.response.send_message("Chọn tầng từ 1 đến 3.", ephemeral=True)
        tier, data = str(tang), load_data()
        removed = data.get("active_registrations", {}).pop(tier, None) is not None
        removed = data.get("pending_challenges", {}).pop(tier, None) is not None or removed
        if not removed:
            return await interaction.response.send_message("Tầng này không có yêu cầu/lịch thách đấu.", ephemeral=True)
        save_data(data)
        await interaction.response.send_message(f"Đã hủy lịch/yêu cầu Tầng {tier}.", ephemeral=True)

    @app_commands.command(name="thach_dau", description="Kiểm tra trạng thái đăng ký thách đấu.")
    @is_gym_channel()
    async def thach_dau(self, interaction: discord.Interaction, tang: int):
        if tang not in (1, 2, 3):
            return await interaction.response.send_message("Chọn tầng từ 1 đến 3.", ephemeral=True)
        session = load_data().get("active_registrations", {}).get(str(tang))
        if not session:
            return await interaction.response.send_message("Tầng hiện không mở đăng ký.", ephemeral=True)
        text = "Bạn đã ghi danh; dùng nút trên thông báo để hủy." if interaction.user.id in session.get("challengers", []) else "Hãy dùng nút Đăng ký / Hủy đăng ký trong thông báo công khai."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="danh_sach_cho", description="Xem danh sách ứng viên đã đăng ký.")
    @is_gym_channel()
    async def danh_sach_cho(self, interaction: discord.Interaction, tang: int):
        tier = str(tang)
        session = load_data().get("active_registrations", {}).get(tier)
        if not session:
            return await interaction.response.send_message(f"Tầng {tang} hiện không mở đăng ký.", ephemeral=True)
        start = local_time(session["start_at"])
        people = session.get("challengers", [])
        embed = discord.Embed(title=f"DANH SÁCH ĐĂNG KÝ TẦNG {tier}", description="\n".join(f"• <@{uid}>" for uid in people) if people else "Chưa có ứng viên.", color=0x3498db)
        embed.add_field(name="Giờ bắt đầu", value=f"{start:%d/%m/%Y %H:%M} giờ Việt Nam")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="tran_dang_dien_ra", description="Xem các trận Dark Gym đang chờ chốt kết quả.")
    @is_gym_channel()
    async def tran_dang_dien_ra(self, interaction: discord.Interaction):
        matches = load_data().get("active_matches", {})
        embed = discord.Embed(title="ACTIVE DARK GYM MATCHES", color=0xe74c3c)
        if not matches:
            embed.description = "Không có trận nào đang diễn ra."
        for tier, match in sorted(matches.items()):
            embed.add_field(name=f"Match #{match['match_id']} — Tầng {tier}", value=f"<@{match['owner_id']}> vs <@{match['challenger_id']}>", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Challenge(bot))
