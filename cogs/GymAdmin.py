import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta
from cogs.core import load_data, save_data, is_gym_channel, get_teambuilding_text

# ID Discord của bạn để phân quyền tối cao cho lệnh thăng chức bằng tay
YOUR_DISCORD_ID = 1031799680792809522  

class GymAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="promote_darkgym", description="[OWNER] Thăng chức bằng tay cho một thành viên lên vị trí Quản Tháp.")
    @is_gym_channel()
    async def promote_darkgym(self, interaction: discord.Interaction, member: discord.User, tang: int):
        # Kiểm tra điều kiện: Chỉ duy nhất tài khoản có ID của bạn mới được sử dụng lệnh này
        if interaction.user.id != YOUR_DISCORD_ID:
            await interaction.response.send_message("❌ Bạn không có quyền hạn tối cao để sử dụng lệnh thăng chức này!", ephemeral=True)
            return

        valid_towers = str("123")
        if str(tang) not in valid_towers:
            await interaction.response.send_message("Tầng không hợp lệ! Hãy chọn tầng từ 1 đến 3.", ephemeral=True)
            return
            
        data = load_data()
        tang_str = str(tang)
        now = datetime.now()
        
        # Tiến hành cập nhật dữ liệu Quản Tháp mới và kích hoạt 12 giờ bảo hộ thành trì
        data["towers"][tang_str]["user_id"] = member.id
        data["towers"][tang_str]["protected_until"] = (now + timedelta(hours=12)).isoformat()
        save_data(data)
        
        embed = discord.Embed(
            title="👑 LỆNH ĐIỀU ĐỘNG QUẢN THÁP TỐI CAO 👑",
            description=f"Sếp {interaction.user.mention} đã ban cơ cấu thành công với thù lao 1 tờ xanh xanh và nụ hôn lốc xoáy kiểu Pháp",
            color=0xf1c40f # Màu vàng hoàng gia
        )
        embed.add_field(name="👑Vị trí ", value=f"**Tầng {tang}** ➔ {data['towers'][tang_str]['title']}", inline=False)
        embed.add_field(name="👑 Tân Quản Tháp", value=member.mention, inline=True)
        embed.add_field(name="🛡️ Thời gian bảo vệ", value="`12 tiếng` *Started*", inline=True)
        embed.set_footer(text="Sắc lệnh có hiệu lực ngay khi được ban bố.")
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="thap_gym", description="Xem danh sách Quản Tháp.")
    @is_gym_channel()
    async def thap_gym(self, interaction: discord.Interaction):
        data = load_data()
        embed = discord.Embed(title="🌌 LUNAR ECLIPSE TOWER 🌌", color=0x2f3136)
        for tier, info in sorted(data["towers"].items()):
            user_mention = f"<@{info['user_id']}>" if info["user_id"] else "*Trống (Đang tuyển chọn)*"
            protection_str = ""
            if info["protected_until"]:
                p_time = datetime.fromisoformat(info["protected_until"])
                if p_time > datetime.now():
                    protection_str = f"\n⚠️ *Bảo hộ đến:* {p_time.strftime('%H:%M - %d/%m/%Y')}"
            embed.add_field(name=f"Tầng {tier} ➔ {info['title']}", value=f"👑 **Quản Tháp:** {user_mention}{protection_str}", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ket_qua_gym", description="[ADMIN] Cập nhật kết quả.")
    @commands.has_permissions(administrator=True)
    @is_gym_channel()
    async def ket_qua_gym(self, interaction: discord.Interaction, tang: int, nguoi_thach_dau: discord.User, ket_qua: str):
        if str(tang) not in "123":
            await interaction.response.send_message("Tầng không hợp lệ!", ephemeral=True)
            return
            
        data = load_data()
        tang_str = str(tang)
        now = datetime.now()
        challenger_id_str = str(nguoi_thach_dau.id)
        current_owner_id = data["towers"][tang_str]["user_id"]
        
        if ket_qua.lower() == "thang":
            data["towers"][tang_str]["user_id"] = nguoi_thach_dau.id
            data["towers"][tang_str]["protected_until"] = (now + timedelta(hours=12)).isoformat()
            save_data(data)
            await interaction.response.send_message(f"🎉 Người thách đấu {nguoi_thach_dau.mention} đã chiếm ngôi Tầng {tang}!")
        elif ket_qua.lower() == "thua":
            data["cooldowns"][f"{challenger_id_str}_{tang}"] = (now + timedelta(hours=48)).isoformat()
            if current_owner_id:
                data["wallets"][str(current_owner_id)] = data["wallets"].get(str(current_owner_id), 0) + 15
            save_data(data)
            await interaction.response.send_message(f" Quản tháp bảo vệ ngôi thành công! {nguoi_thach_dau.mention} bị cấm phục thù 48 giờ.")

    @app_commands.command(name="teambuilding", description="Xem quy định về cách xây dựng đội hình thi đấu.")
    @is_gym_channel()
    async def teambuilding(self, interaction: discord.Interaction):
        embed = discord.Embed(title="📝 QUY ĐỊNH TEAMBUILDING - THÁNH ĐỊA BÓNG ĐÊM", color=0x71368a)
        embed.description = get_teambuilding_text()
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(GymAdmin(bot))
