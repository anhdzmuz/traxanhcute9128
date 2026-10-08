import random
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

from cogs.core import (
    GYM_CHANNEL_ID, get_teambuilding_text, is_gym_channel, is_owner,
    load_data, save_data, format_remaining,
)


class Challenge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_challenge_cloisters.start()

    def cog_unload(self):
        self.check_challenge_cloisters.cancel()

    @tasks.loop(minutes=5)
    async def check_challenge_cloisters(self):
        data = load_data()
        now = datetime.now()
        updated = False
        active_sessions = list(data.get("active_registrations", {}).items())

        for tier, session in active_sessions:
            try:
                end_time = datetime.fromisoformat(session["end_at"])
            except (KeyError, ValueError):
                del data["active_registrations"][tier]
                updated = True
                continue

            if now < end_time:
                continue

            channel = self.bot.get_channel(GYM_CHANNEL_ID)
            challengers = session.get("challengers", [])
            tower_info = data["towers"][tier]
            owner_id = tower_info.get("user_id")

            if not challengers:
                if channel:
                    await channel.send(f"⏳ **Thời gian đăng ký Tầng {tier} đã hết.** Không có Trainer nào nộp đơn thách đấu!")
                del data["active_registrations"][tier]
                updated = True
                continue

            if not owner_id:
                if channel:
                    await channel.send(f"⚠️ Tầng {tier} chưa có Quản Tháp, phiên đăng ký đã bị hủy.")
                del data["active_registrations"][tier]
                updated = True
                continue

            if tier in data["active_matches"]:
                if channel:
                    await channel.send(f"⚠️ Tầng {tier} đã có trận đang diễn ra. Phiên đăng ký bị hủy để tránh ghi đè dữ liệu.")
                del data["active_registrations"][tier]
                updated = True
                continue

            lucky = random.choice(challengers)
            match_id = self._next_match_id(data)
            data["active_matches"][tier] = {
                "match_id": match_id,
                "tower": int(tier),
                "owner_id": int(owner_id),
                "challenger_id": int(lucky),
                "created_at": now.isoformat(),
                "owner_time": session.get("owner_time", "Chưa đặt"),
                "challenger_time": session.get("challenger_time", "Chưa đặt"),
            }
            del data["active_registrations"][tier]
            updated = True

            if channel:
                embed = discord.Embed(
                    title=f"⚔️ MATCH #{match_id} — BỐC THĂM TẦNG {tier}",
                    description=f"Hệ thống đã chọn **1/{len(challengers)}** ứng viên. Trận đấu chính thức được mở!",
                    color=0xe74c3c,
                )
                embed.add_field(name="👑 Quản Tháp", value=f"<@{owner_id}>", inline=True)
                embed.add_field(name="🗡️ Người Thách Đấu", value=f"<@{lucky}>", inline=True)
                embed.add_field(name="🕒 Khung giờ Quản Tháp", value=f"`{session.get('owner_time', 'Chưa đặt')}`", inline=False)
                embed.add_field(name="🕒 Khung giờ Challenger", value=f"`{session.get('challenger_time', 'Chưa đặt')}`", inline=False)
                embed.add_field(name="📜 Teambuilding", value=get_teambuilding_text(), inline=False)
                embed.set_footer(text=f"Chốt kết quả bằng /ket_qua_gym tang:{tier} | Owner ID: 1031799680792809522")
                await channel.send(content=f"🔔 Chúc mừng <@{lucky}>! Bạn đã trúng suất thi đấu với <@{owner_id}>.", embed=embed)

        if updated:
            save_data(data)

    @check_challenge_cloisters.before_loop
    async def before_challenge_loop(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def _next_match_id(data):
        history_ids = [int(x.get("match_id", 0)) for x in data.get("match_history", []) if str(x.get("match_id", "")).isdigit()]
        active_ids = [int(x.get("match_id", 0)) for x in data.get("active_matches", {}).values() if str(x.get("match_id", "")).isdigit()]
        return max(history_ids + active_ids + [0]) + 1

    async def owner_only(self, interaction):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Chỉ Owner của Dark Gym mới có quyền sử dụng lệnh này!", ephemeral=True)
            return False
        return True

    @app_commands.command(name="mo_thach_dau", description="[OWNER] Mở đăng ký thách đấu trong 12 giờ.")
    @is_gym_channel()
    async def mo_thach_dau(self, interaction: discord.Interaction, tang: int, khung_gio_quan_thap: str, khung_gio_nguoi_dau: str):
        if not await self.owner_only(interaction):
            return
        if str(tang) not in "123":
            await interaction.response.send_message("❌ Tầng không hợp lệ! Chọn tầng 1 đến 3.", ephemeral=True)
            return

        data = load_data()
        tier = str(tang)
        now = datetime.now()
        tower = data["towers"][tier]

        if not tower.get("user_id"):
            await interaction.response.send_message("❌ Tầng này chưa có Quản Tháp.", ephemeral=True)
            return
        if tier in data["active_registrations"]:
            await interaction.response.send_message("⚠️ Tầng này đang có một phiên đăng ký.", ephemeral=True)
            return
        if tier in data["active_matches"]:
            await interaction.response.send_message("⚔️ Tầng này đang có một trận đấu chưa chốt kết quả.", ephemeral=True)
            return
        if tower.get("protected_until"):
            protection = datetime.fromisoformat(tower["protected_until"])
            if protection > now:
                await interaction.response.send_message(
                    f"🛡️ Tầng {tang} đang được bảo hộ. Còn **{format_remaining(protection - now)}**.", ephemeral=True
                )
                return

        end_time = now + timedelta(hours=12)
        data["active_registrations"][tier] = {
            "owner_time": khung_gio_quan_thap,
            "challenger_time": khung_gio_nguoi_dau,
            "end_at": end_time.isoformat(),
            "challengers": [],
        }
        save_data(data)

        embed = discord.Embed(
            title=f"📢 BẮT ĐẦU NHẬN LỜI THÁCH ĐẤU — TẦNG {tang}",
            description="Cổng đăng ký đã mở. Sau **12 giờ**, hệ thống tự động bốc thăm và tạo Match.",
            color=0x2ecc71,
        )
        embed.add_field(name="👑 Quản Tháp", value=f"<@{tower['user_id']}> — `{khung_gio_quan_thap}`", inline=False)
        embed.add_field(name="🗡️ Challenger", value=f"`{khung_gio_nguoi_dau}`", inline=False)
        embed.add_field(name="⏰ Đóng đăng ký", value=f"`{end_time.strftime('%H:%M - %d/%m/%Y')}`", inline=False)
        embed.add_field(name="✍️ Đăng ký", value=f"Dùng `/thach_dau tang:{tang}`", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="huy_thach_dau", description="[OWNER] Hủy phiên đăng ký của một tầng.")
    @is_gym_channel()
    async def huy_thach_dau(self, interaction: discord.Interaction, tang: int):
        if not await self.owner_only(interaction):
            return
        tier = str(tang)
        data = load_data()
        if tier not in data.get("active_registrations", {}):
            await interaction.response.send_message(f"❌ Tầng {tang} không có phiên đăng ký.", ephemeral=True)
            return
        del data["active_registrations"][tier]
        save_data(data)
        await interaction.response.send_message(f"🛑 Đã đóng phiên đăng ký Tầng {tang}.")

    @app_commands.command(name="thach_dau", description="Đăng ký suất thách đấu tại một tầng đang mở.")
    @is_gym_channel()
    async def thach_dau(self, interaction: discord.Interaction, tang: int):
        if str(tang) not in "123":
            await interaction.response.send_message("❌ Tầng không hợp lệ!", ephemeral=True)
            return

        data = load_data()
        tier = str(tang)
        user_id = interaction.user.id
        now = datetime.now()
        session = data.get("active_registrations", {}).get(tier)

        if not session:
            await interaction.response.send_message("❌ Tầng hiện đang đóng đăng ký.", ephemeral=True)
            return
        if data["towers"][tier].get("user_id") == user_id:
            await interaction.response.send_message("❌ Bạn đang là Quản Tháp của tầng này.", ephemeral=True)
            return
        if tier in data.get("active_matches", {}):
            await interaction.response.send_message("⚔️ Tầng đang có trận đấu.", ephemeral=True)
            return

        cd_key = f"{user_id}_{tier}"
        if cd_key in data["cooldowns"]:
            cd_time = datetime.fromisoformat(data["cooldowns"][cd_key])
            if now < cd_time:
                await interaction.response.send_message(
                    f"❌ Bạn đang cooldown phục thù. Còn **{format_remaining(cd_time - now)}**.", ephemeral=True
                )
                return
            del data["cooldowns"][cd_key]

        protection = data["towers"][tier].get("protected_until")
        if protection:
            p_time = datetime.fromisoformat(protection)
            if now < p_time:
                await interaction.response.send_message(
                    f"🛡️ Tầng đang được bảo hộ đến `{p_time.strftime('%H:%M - %d/%m/%Y')}`.", ephemeral=True
                )
                return

        if user_id in session["challengers"]:
            await interaction.response.send_message("⚠️ Bạn đã đăng ký tầng này rồi.", ephemeral=True)
            return

        session["challengers"].append(user_id)
        save_data(data)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} đã ghi danh Tầng {tang}. Chúc may mắn!"
        )

    @app_commands.command(name="danh_sach_cho", description="Xem danh sách ứng viên đang chờ bốc thăm.")
    @is_gym_channel()
    async def danh_sach_cho(self, interaction: discord.Interaction, tang: int):
        tier = str(tang)
        data = load_data()
        session = data.get("active_registrations", {}).get(tier)
        if not session:
            await interaction.response.send_message(f"📊 Tầng {tang} hiện đang đóng đăng ký.", ephemeral=True)
            return

        end_time = datetime.fromisoformat(session["end_at"])
        challengers = session.get("challengers", [])
        embed = discord.Embed(title=f"📋 DANH SÁCH ĐĂNG KÝ TẦNG {tang}", color=0x3498db)
        embed.add_field(name="⏰ Đóng đăng ký", value=f"`{end_time.strftime('%H:%M - %d/%m/%Y')}`", inline=False)
        if challengers:
            embed.description = f"**{len(challengers)} ứng viên:**\n" + "\n".join(f"🔹 <@{uid}>" for uid in challengers)
        else:
            embed.description = "*Chưa có Trainer nào ghi danh.*"
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="tran_dang_dien_ra", description="Xem các trận Dark Gym đang chờ chốt kết quả.")
    @is_gym_channel()
    async def tran_dang_dien_ra(self, interaction: discord.Interaction):
        data = load_data()
        matches = data.get("active_matches", {})
        embed = discord.Embed(title="⚔️ ACTIVE DARK GYM MATCHES", color=0xe74c3c)
        if not matches:
            embed.description = "*Không có trận nào đang diễn ra.*"
        else:
            for tier, match in sorted(matches.items()):
                embed.add_field(
                    name=f"Match #{match['match_id']} — Tầng {tier}",
                    value=f"👑 <@{match['owner_id']}> vs 🗡️ <@{match['challenger_id']}>\nDùng `/ket_qua_gym` để chốt trận.",
                    inline=False,
                )
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Challenge(bot))
