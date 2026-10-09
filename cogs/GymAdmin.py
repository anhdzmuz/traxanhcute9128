import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

from cogs.core import (
    OWNER_ID, award_achievements, get_player, get_teambuilding_text,
    is_gym_channel, is_owner, load_data, save_data,
)


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
    async def promote_darkgym(self, interaction: discord.Interaction, member: discord.User, tang: int):
        if not await self.owner_only(interaction):
            return
        if tang not in (1, 2, 3):
            await interaction.response.send_message("❌ Tầng không hợp lệ! Chọn tầng 1 đến 3.", ephemeral=True)
            return

        data = load_data()
        tier = str(tang)
        old_owner = data["towers"][tier].get("user_id")
        data["towers"][tier]["user_id"] = member.id
        data["towers"][tier]["protected_until"] = (datetime.now(TZ) + timedelta(hours=12)).isoformat()
        data["towers"][tier]["defense_streak"] = 0
        # A promotion changes the canonical tower owner; clear any stale request or signup tied to the previous owner.
        data.get("pending_challenges", {}).pop(tier, None)
        data.get("active_registrations", {}).pop(tier, None)
        save_data(data)

        embed = discord.Embed(title="👑 LỆNH ĐIỀU ĐỘNG QUẢN THÁP", color=0xf1c40f)
        embed.description = f"{interaction.user.mention} đã chỉ định Quản Tháp mới."
        embed.add_field(name="🏰 Tầng", value=f"**{tang}** — {data['towers'][tier]['title']}", inline=False)
        embed.add_field(name="👑 Tân Quản Tháp", value=member.mention, inline=True)
        embed.add_field(name="🛡️ Bảo hộ", value="12 giờ", inline=True)
        if old_owner:
            embed.add_field(name="📜 Người tiền nhiệm", value=f"<@{old_owner}>", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="xoa_bao_ho", description="[OWNER] Xóa bảo hộ của một tầng.")
    @is_gym_channel()
    async def xoa_bao_ho(self, interaction: discord.Interaction, tang: int):
        if not await self.owner_only(interaction):
            return
        if tang not in (1, 2, 3):
            return await interaction.response.send_message("❌ Tầng không hợp lệ! Chọn tầng 1 đến 3.", ephemeral=True)

        data = load_data()
        tier = str(tang)
        tower = data["towers"][tier]
        if not tower.get("protected_until"):
            return await interaction.response.send_message(f"ℹ️ Tầng {tang} hiện không có thời gian bảo hộ được lưu.", ephemeral=True)

        tower["protected_until"] = None
        save_data(data)
        await interaction.response.send_message(
            f"🛡️ Owner đã xóa bảo hộ của **Tầng {tang} — {tower['title']}**. Tầng này có thể được thách đấu nếu không vướng điều kiện khác."
        )

    @app_commands.command(name="thap_gym", description="Xem trạng thái 3 tầng Dark Gym.")
    @is_gym_channel()
    async def thap_gym(self, interaction: discord.Interaction):
        data = load_data()
        embed = discord.Embed(title="🌌 LUNAR ECLIPSE TOWER 🌌", color=0x2f3136)
        for tier, info in sorted(data["towers"].items()):
            owner = info.get("user_id")
            owner_text = f"<@{owner}>" if owner else "*Trống (Đang tuyển chọn)*"
            protection = ""
            if info.get("protected_until"):
                p_time = datetime.fromisoformat(info["protected_until"])
                if p_time.tzinfo is None:
                    p_time = p_time.replace(tzinfo=TZ)
                if p_time > datetime.now(TZ):
                    protection = f"\n🛡️ Bảo hộ đến: `{p_time.strftime('%H:%M - %d/%m/%Y')}`"
            streak = info.get("defense_streak", 0)
            embed.add_field(
                name=f"Tầng {tier} ➔ {info['title']}",
                value=f"👑 **Quản Tháp:** {owner_text}\n🔥 Defense Streak: **{streak}**{protection}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ket_qua_gym", description="[OWNER] Chốt kết quả trận đang diễn ra.")
    @app_commands.choices(ket_qua=[
        app_commands.Choice(name="Challenger thắng", value="thang"),
        app_commands.Choice(name="Quản Tháp thắng", value="thua"),
    ])
    @is_gym_channel()
    async def ket_qua_gym(self, interaction: discord.Interaction, tang: int, ket_qua: app_commands.Choice[str]):
        if not await self.owner_only(interaction):
            return
        if str(tang) not in "123":
            await interaction.response.send_message("❌ Tầng không hợp lệ!", ephemeral=True)
            return

        data = load_data()
        tier = str(tang)
        match = data["active_matches"].get(tier)
        if not match:
            await interaction.response.send_message(f"❌ Tầng {tang} hiện không có trận đấu đang diễn ra.", ephemeral=True)
            return

        challenger_id = int(match["challenger_id"])
        owner_id = int(match["owner_id"])
        tower = data["towers"][tier]
        now = datetime.now(TZ)

        challenger = get_player(data, challenger_id)
        owner = get_player(data, owner_id)
        challenger["win_streak"] = challenger.get("win_streak", 0)
        owner["win_streak"] = owner.get("win_streak", 0)

        if ket_qua.value == "thang":
            challenger["wins"] += 1
            challenger["captures"] += 1
            challenger["win_streak"] += 1
            challenger["best_win_streak"] = max(challenger["best_win_streak"], challenger["win_streak"])
            if tier not in challenger["towers_captured"]:
                challenger["towers_captured"].append(tier)
            owner["losses"] += 1
            owner["win_streak"] = 0
            tower["user_id"] = challenger_id
            tower["protected_until"] = (now + timedelta(hours=12)).isoformat()
            tower["defense_streak"] = 0
            result_text = f"🏆 <@{challenger_id}> đã **chiếm ngôi** Tầng {tang}!"
        else:
            challenger["losses"] += 1
            challenger["win_streak"] = 0
            owner["wins"] += 1
            owner["defenses"] += 1
            owner["win_streak"] += 1
            owner["best_win_streak"] = max(owner["best_win_streak"], owner["win_streak"])
            tower["defense_streak"] = tower.get("defense_streak", 0) + 1
            data["wallets"][str(owner_id)] = data["wallets"].get(str(owner_id), 0) + 15
            data["cooldowns"][f"{challenger_id}_{tang}"] = (now + timedelta(hours=48)).isoformat()
            result_text = f"🛡️ <@{owner_id}> đã **phòng thủ thành công** Tầng {tang}!"

        new_achievements = []
        new_achievements.extend(award_achievements(data, challenger_id))
        new_achievements.extend(award_achievements(data, owner_id))

        match_id = match["match_id"]
        data["match_history"].append({
            "match_id": match_id,
            "tower": tang,
            "challenger_id": challenger_id,
            "owner_id": owner_id,
            "result": ket_qua.value,
            "ended_at": now.strftime("%Y-%m-%d %H:%M"),
        })
        data["match_history"] = data["match_history"][-100:]
        del data["active_matches"][tier]
        save_data(data)

        embed = discord.Embed(title=f"⚔️ KẾT QUẢ MATCH #{match_id}", description=result_text, color=0x2ecc71 if ket_qua.value == "thang" else 0x3498db)
        embed.add_field(name="🏰 Tầng", value=f"{tang} — {tower['title']}", inline=True)
        embed.add_field(name="🗡️ Challenger", value=f"<@{challenger_id}>", inline=True)
        embed.add_field(name="👑 Quản Tháp cũ", value=f"<@{owner_id}>", inline=True)
        if ket_qua.value == "thua":
            embed.add_field(name="✨ Phần thưởng", value="Quản Tháp nhận **+15 Dark Stardust**\nChallenger cooldown **48 giờ**", inline=False)
        else:
            embed.add_field(name="🛡️ Bảo hộ", value="Quản Tháp mới được bảo hộ **12 giờ**", inline=False)
        if new_achievements:
            embed.add_field(name="🏅 Achievement mới", value="\n".join(set(new_achievements)), inline=False)
        await interaction.response.send_message(embed=embed)



async def setup(bot):
    await bot.add_cog(GymAdmin(bot))
