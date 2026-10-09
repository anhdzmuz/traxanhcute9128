import discord
from discord.ext import commands
from discord import app_commands

from .core import get_player, get_achievement_names, is_gym_channel, load_data


class Profile(commands.Cog):
    """Lệnh profile Dark Gym, tách riêng khỏi core.py."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="profile", description="Hiển thị profile Dark Gym của bạn hoặc người được chọn.")
    @is_gym_channel()
    async def dark_profile(self, interaction: discord.Interaction, member: discord.User | None = None):
        target = member or interaction.user
        data = load_data()
        player = get_player(data, target.id)
        balance = data["wallets"].get(str(target.id), 0)
        achievements = get_achievement_names(player)
        total = player["wins"] + player["losses"]
        win_rate = (player["wins"] / total * 100) if total else 0
        title = achievements[-1] if achievements else "🌑 Dark Trainer"

        embed = discord.Embed(
            title=f"✦ {target.display_name} · DARK PROFILE",
            description=(
                f"**{title}**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ **Defense Streak**　`{player.get('defense_streak', player['defenses'])}`　·　"
                f"🔥 **Win Streak**　`{player['win_streak']}`\n"
                f"📈 **Win Rate**　`{win_rate:.1f}%`"
            ),
            color=0x252936,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="✦ STAR DUSK", value=f"**{balance:,}**", inline=True)
        embed.add_field(name="⚔️ WIN", value=f"**{player['wins']:,}**", inline=True)
        embed.add_field(name="☠️ LOSE", value=f"**{player['losses']:,}**", inline=True)
        embed.add_field(name="👑 Danh hiệu", value=title, inline=False)
        embed.add_field(
            name="🏅 Thành tựu",
            value="　•　".join(achievements) if achievements else "*Chưa mở khóa thành tựu nào*",
            inline=False,
        )
        embed.set_footer(text=f"DARK GYM  •  {player['captures']} lần chiếm tháp  •  {player['defenses']} lần phòng thủ")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
