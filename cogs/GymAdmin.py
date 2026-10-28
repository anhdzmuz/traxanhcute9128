import discord
from discord import app_commands
from discord.ext import commands

import gym_logic as gl
from cogs.core import fail_deferred, is_gym_channel, is_owner, read_data, update_data

TierOption = app_commands.Range[int, 1, 3]


class GymAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def owner_only(self, interaction):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Chỉ Owner của Dark Gym mới có quyền sử dụng lệnh này!", ephemeral=True)
            return False
        return True

    @app_commands.command(name="promote_darkgym", description="[OWNER] Chỉ định Quản Tháp cho một tầng.")
    @is_gym_channel()
    async def promote_darkgym(self, interaction: discord.Interaction, member: discord.User, tang: TierOption):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer()
        tier, current = str(tang), gl.now()
        status, payload = await update_data(lambda d: gl.promote(d, tier, member.id, current))
        if status == "match":
            return await fail_deferred(
                interaction,
                f"❌ Tầng {tang} đang có trận chưa chốt kết quả. Hãy dùng /ket_qua_gym trước khi đổi Quản Tháp.",
            )

        embed = discord.Embed(title="👑 LỆNH ĐIỀU ĐỘNG QUẢN THÁP", color=0xf1c40f)
        embed.description = f"{interaction.user.mention} đã chỉ định Quản Tháp mới."
        embed.add_field(name="🏰 Tầng", value=f"**{tang}** — {payload['title']}", inline=False)
        embed.add_field(name="👑 Tân Quản Tháp", value=member.mention, inline=True)
        embed.add_field(name="🛡️ Bảo hộ", value="12 giờ", inline=True)
        if payload["old_owner"]:
            embed.add_field(name="📜 Người tiền nhiệm", value=f"<@{payload['old_owner']}>", inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="xoa_bao_ho", description="[OWNER] Xóa bảo hộ của một tầng.")
    @is_gym_channel()
    async def xoa_bao_ho(self, interaction: discord.Interaction, tang: TierOption):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer()
        tier = str(tang)
        status, title = await update_data(lambda d: gl.clear_protection(d, tier))
        if status == "none":
            return await fail_deferred(interaction, f"ℹ️ Tầng {tang} hiện không có thời gian bảo hộ được lưu.")
        await interaction.followup.send(
            f"🛡️ Owner đã xóa bảo hộ của **Tầng {tang} — {title}**. Tầng này có thể được thách đấu nếu không vướng điều kiện khác."
        )

    @app_commands.command(name="thap_gym", description="Xem trạng thái 3 tầng Dark Gym.")
    @is_gym_channel()
    async def thap_gym(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await read_data()
        current = gl.now()
        embed = discord.Embed(title="🌌 LUNAR ECLIPSE TOWER 🌌", color=0x2f3136)
        for tier, info in sorted(data["towers"].items()):
            owner = info.get("user_id")
            owner_text = f"<@{owner}>" if owner else "*Trống (Đang tuyển chọn)*"
            protection = ""
            if info.get("protected_until"):
                try:
                    p_time = gl.parse_dt(info["protected_until"])
                except (TypeError, ValueError):
                    p_time = None
                if p_time and p_time > current:
                    protection = f"\n🛡️ Bảo hộ đến: `{p_time.strftime('%H:%M - %d/%m/%Y')}`"
            streak = info.get("defense_streak", 0)
            embed.add_field(
                name=f"Tầng {tier} ➔ {info['title']}",
                value=f"👑 **Quản Tháp:** {owner_text}\n🔥 Defense Streak: **{streak}**{protection}",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="ket_qua_gym", description="[OWNER] Chốt kết quả trận đang diễn ra.")
    @app_commands.choices(ket_qua=[
        app_commands.Choice(name="Challenger thắng", value="thang"),
        app_commands.Choice(name="Quản Tháp thắng", value="thua"),
    ])
    @is_gym_channel()
    async def ket_qua_gym(self, interaction: discord.Interaction, tang: TierOption, ket_qua: app_commands.Choice[str]):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer()
        tier, outcome, current = str(tang), ket_qua.value, gl.now()
        # Toàn bộ việc chốt trận nằm trong một giao dịch: bấm hai lần cũng không bị tính đôi.
        status, info = await update_data(lambda d: gl.record_result(d, tier, outcome, current))
        if status == "no_match":
            return await fail_deferred(interaction, f"❌ Tầng {tang} hiện không có trận đấu đang diễn ra.")

        challenger_id, owner_id = info["challenger_id"], info["owner_id"]
        if outcome == "thang":
            result_text = f"🏆 <@{challenger_id}> đã **chiếm ngôi** Tầng {tang}!"
        else:
            result_text = f"🛡️ <@{owner_id}> đã **phòng thủ thành công** Tầng {tang}!"

        embed = discord.Embed(
            title=f"⚔️ KẾT QUẢ MATCH #{info['match_id']}",
            description=result_text,
            color=0x2ecc71 if outcome == "thang" else 0x3498db,
        )
        embed.add_field(name="🏰 Tầng", value=f"{tang} — {info['title']}", inline=True)
        embed.add_field(name="🗡️ Challenger", value=f"<@{challenger_id}>", inline=True)
        embed.add_field(name="👑 Quản Tháp cũ", value=f"<@{owner_id}>", inline=True)
        if outcome == "thua":
            embed.add_field(
                name="✨ Phần thưởng",
                value=f"Quản Tháp nhận **+{gl.DEFENSE_REWARD} Dark Stardust**\nChallenger cooldown **48 giờ**",
                inline=False,
            )
        else:
            embed.add_field(name="🛡️ Bảo hộ", value="Quản Tháp mới được bảo hộ **12 giờ**", inline=False)
        if info["achievements"]:
            embed.add_field(name="🏅 Achievement mới", value="\n".join(info["achievements"]), inline=False)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(GymAdmin(bot))
