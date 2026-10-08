import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
from datetime import datetime, timedelta
from cogs.core import load_data, save_data, is_gym_channel, GYM_CHANNEL_ID, get_teambuilding_text

YOUR_DISCORD_ID = 1031799680792809522  

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
        
        if "active_registrations" not in data:
            data["active_registrations"] = {}
            save_data(data)
            return

        active_sessions = list(data["active_registrations"].items())
        
        for tang_str, session in active_sessions:
            end_time = datetime.fromisoformat(session["end_at"])
            if now >= end_time:
                channel = self.bot.get_channel(GYM_CHANNEL_ID)
                challengers = session["challengers"]
                tower_info = data["towers"][tang_str]
                owner_id = tower_info["user_id"]
                owner_mention = f"<@{owner_id}>" if owner_id else "*(Trống)*"
                
                if not challengers:
                    if channel:
                        await channel.send(f"⏳ **Thời gian đăng ký Tầng {tang_str} đã hết.** Không có Trainer nào nộp đơn thách đấu!")
                else:
                    lucky_challenger_id = random.choice(challengers)
                    if channel:
                        embed = discord.Embed(
                            title=f"⚔️ KẾT QUẢ BỐC THĂM THÁCH ĐẤU TẦNG {tang_str} ⚔️",
                            description=f"Hệ thống đã chọn ngẫu nhiên người thách đấu may mắn từ `{len(challengers)}` ứng viên báo danh!",
                            color=0xe74c3c
                        )
                        embed.add_field(name="👑 Quản Tháp", value=owner_mention, inline=True)
                        embed.add_field(name="🗡️ Người Thách Đấu", value=f"<@{lucky_challenger_id}>", inline=True)
                        embed.add_field(name="🕒 Khung giờ Quản Tháp nhận trận", value=f"`{session['owner_time']}`", inline=False)
                        embed.add_field(name="🕒 Khung giờ Người thách đấu đánh", value=f"`{session['challenger_time']}`", inline=False)
                        embed.add_field(name="📜 Quy chế Teambuilding", value=get_teambuilding_text(), inline=False)
                        
                        await channel.send(content=f"🔔 Chúc mừng <@{lucky_challenger_id}> trúng suất thi đấu với Quản tháp {owner_mention}!", embed=embed)
                
                del data["active_registrations"][tang_str]
                updated = True
                
        if updated:
            save_data(data)

    @app_commands.command(name="mo_thach_dau", description="Bắt đầu nhận lời thách đấu cho một tầng trong vòng 12 tiếng.")
    @is_gym_channel()
    async def mo_thach_dau(self, interaction: discord.Interaction, tang: int, khung_gio_quan_thap: str, khung_gio_nguoi_dau: str):
        if interaction.user.id != YOUR_DISCORD_ID and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Bạn không có quyền sử dụng lệnh quản lý giải này!", ephemeral=True)
            return

        if str(tang) not in "123":
            await interaction.response.send_message("Tầng không hợp lệ! Hãy chọn tầng từ 1 đến 3.", ephemeral=True)
            return

        data = load_data()
        tang_str = str(tang)
        now = datetime.now()
        end_time = now + timedelta(hours=12)
        
        data["active_registrations"][tang_str] = {
            "owner_time": khung_gio_quan_thap,
            "challenger_time": khung_gio_nguoi_dau,
            "end_at": end_time.isoformat(),
            "challengers": []
        }
        save_data(data)
        
        embed = discord.Embed(
            title=f"📢 BẮT ĐẦU NHẬN LỜI THÁCH ĐẤU - TẦNG {tang}",
            description="Cổng đăng ký thách đấu đã mở! Hệ thống sẽ đóng đơn và tự động bốc thăm sau **12 giờ**.",
            color=0x2ecc71
        )
        embed.add_field(name="👑 Quản tháp nhận trận", value=f"`{khung_gio_quan_thap}` (Khung 2 tiếng)", inline=False)
        embed.add_field(name="🗡️ Người thách đấu có thể đánh", value=f"`{khung_gio_nguoi_dau}` (Khung 2 tiếng)", inline=False)
        embed.add_field(name="⏰ Thời gian đóng hòm phiếu", value=f"{end_time.strftime('%H:%M - %d/%m/%Y')}", inline=False)
        embed.add_field(name="✍️ Cách thức tham gia", value=f"Gõ lệnh `/thach_dau` và chọn tầng `{tang}` để ghi danh!", inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="huy_thach_dau", description="Hủy phiên nhận đăng ký thách đấu của một tầng.")
    @is_gym_channel()
    async def huy_thach_dau(self, interaction: discord.Interaction, tang: int):
        if interaction.user.id != YOUR_DISCORD_ID and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Bạn không có quyền sử dụng lệnh này!", ephemeral=True)
            return

        data = load_data()
        tang_str = str(tang)
        
        if tang_str not in data.get("active_registrations", {}):
            await interaction.response.send_message(f"❌ Tầng {tang} hiện tại không có phiên đăng ký nào đang mở!", ephemeral=True)
            return
            
        del data["active_registrations"][tang_str]
        save_data(data)
        await interaction.response.send_message(f"🛑 Đã hủy và đóng cổng đăng ký bốc thăm cho Tầng {tang} thành công!")

    @app_commands.command(name="thach_dau", description="Ghi danh đăng ký suất thách đấu may mắn.")
    @is_gym_channel()
    async def thach_dau(self, interaction: discord.Interaction, tang: int):
        if str(tang) not in "123":
            await interaction.response.send_message("Tầng không hợp lệ! Hãy chọn tầng từ 1 đến 3.", ephemeral=True)
            return
        
        data = load_data()
        tang_str = str(tang)
        user_id = interaction.user.id
        user_id_str = str(user_id)
        now = datetime.now()
        
        if tang_str not in data.get("active_registrations", {}):
            await interaction.response.send_message(f"❌ Tầng {tang} hiện tại đang đóng, chưa mở nhận đơn thách đấu từ Admin!", ephemeral=True)
            return
            
        cd_key = f"{user_id_str}_{tang}"
        if cd_key in data["cooldowns"]:
            cd_time = datetime.fromisoformat(data["cooldowns"][cd_key])
            if now < cd_time:
                remaining = cd_time - now
                hours, remainder = divmod(remaining.seconds, 3600)
                await interaction.response.send_message(f"❌ Bạn đang trong thời gian hồi chiêu phục thù! Còn {remaining.days} ngày {hours} giờ.", ephemeral=True)
                return

        tower_info = data["towers"][tang_str]
        if tower_info["protected_until"]:
            p_time = datetime.fromisoformat(tower_info["protected_until"])
            if now < p_time:
                await interaction.response.send_message(f"🛡️ Quản Tháp tầng này đang được bảo hộ đến: `{p_time.strftime('%H:%M - %d/%m/%Y')}`. Không thể thách đấu!", ephemeral=True)
                return

        if user_id in data["active_registrations"][tang_str]["challengers"]:
            await interaction.response.send_message(f"⚠️ Bạn đã có tên trong danh sách đăng ký Tầng {tang} rồi, vui lòng đợi hệ thống bốc thăm!", ephemeral=True)
            return
            
        data["active_registrations"][tang_str]["challengers"].append(user_id)
        save_data(data)
        await interaction.response.send_message(f"✅ Ghi danh thành công! Trainer {interaction.user.mention} đã tham gia vào hàng chờ bốc thăm Tầng {tang}.", ephemeral=False)

    @app_commands.command(name="danh_sach_cho", description="Xem danh sách các ứng cử viên đang xếp hàng chờ bốc thăm.")
    @is_gym_channel()
    async def danh_sach_cho(self, interaction: discord.Interaction, tang: int):
        if str(tang) not in "123":
            await interaction.response.send_message("Tầng không hợp lệ!", ephemeral=True)
            return
            
        data = load_data()
        tang_str = str(tang)
        
        if tang_str not in data.get("active_registrations", {}):
            await interaction.response.send_message(f"📊 Tầng {tang} hiện tại đang đóng đăng ký.", ephemeral=True)
            return
            
        session = data["active_registrations"][tang_str]
        challengers = session["challengers"]
        end_time = datetime.fromisoformat(session["end_at"])
        
        embed = discord.Embed(title=f"📋 DANH SÁCH ĐĂNG KÝ TẦNG {tang}", color=0x3498db)
        embed.add_field(name="⏰ Thời gian chốt hòm phiếu", value=f"{end_time.strftime('%H:%M - %d/%m/%Y')}", inline=False)
        
        if not challengers:
            embed.description = "*Chưa có Trainer nào ghi danh tham gia đấu tầng này.*"
        else:
            mentions = [f"🔹 <@{uid}>" for uid in challengers]
            embed.description = f"**Tổng số ứng viên:** `{len(challengers)}` người\n\n" + "\n".join(mentions)
            
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Challenge(bot))
