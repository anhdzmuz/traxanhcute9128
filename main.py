import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import random
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

# ========================================================
# 🌐 CẤU HÌNH WEB SERVER ĐỂ GIỮ BOT LUÔN THỨC
app = Flask('')

@app.route('/')
def home():
    return "Thánh Địa Bóng Đêm đang hoạt động ổn định!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()
# ========================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

DATA_FILE = "gym_data.json"
GYM_CHANNEL_ID = 1147411953501880390  

# 🔒 HÃY THAY ID DISCORD CỦA BẠN VÀO ĐÂY ĐỂ CHẠY LỆNH /mo_thach_dau
YOUR_DISCORD_ID = 1031799680792809522  

default_data = {
    "towers": {
        "1": {"title": "Antumbra Overlord", "user_id": None, "protected_until": None},
        "2": {"title": "Umbra Weaver", "user_id": None, "protected_until": None},
        "3": {"title": "Penumbra Sentinel", "user_id": None, "protected_until": None}
    },
    "wallets": {},
    "cooldowns": {},
    "active_registrations": {}
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(default_data, f, indent=4)
        return default_data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            content = json.load(f)
            if "active_registrations" not in content:
                content["active_registrations"] = {}
            return content
        except Exception:
            return default_data

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def is_gym_channel():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.channel_id == GYM_CHANNEL_ID
    return app_commands.check(predicate)

def get_teambuilding_text():
    return (
        "⚠️ **LƯU Ý QUY ĐỊNH TEAMBUILDING:**\n"
        "• **Core Hệ Dark:** Cả Quản tháp & Người thách đấu bắt buộc mang tối thiểu **3/6 Pokémon** hệ Dark.\n"
        "• **Hybrid Team Sheet:** *'Ánh sáng phơi bày chiến thuật, bóng tối định đoạt thắng thua'*\n"
        "  ➔ Chỉ công khai (Move, Nature, Item, Gender) của **4/6 Pokémon** trong đội.\n"
        "  ➔ **2 Pokémon còn lại** sẽ được giấu kín hoàn toàn mọi thông tin trên."
    )

@tasks.loop(hours=3)
async def reward_stardust_loop():
    data = load_data()
    updated = False
    for tier, info in data["towers"].items():
        user_id = info["user_id"]
        if user_id:
            user_id_str = str(user_id)
            data["wallets"][user_id_str] = data["wallets"].get(user_id_str, 0) + 10
            updated = True
    if updated:
        save_data(data)

@tasks.loop(minutes=5)
async def check_challenge_cloisters():
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
            channel = bot.get_channel(GYM_CHANNEL_ID)
            challengers = session["challengers"]
            tower_info = data["towers"][tang_str]
            owner_id = tower_info["user_id"]
            owner_mention = f"<@{owner_id}>" if owner_id else "*(Trống)*"
            
            if not challengers:
                if channel:
                    await channel.send(f"⏳ **Thời gian đăng ký Tầng {tang_str} đã hết.** Không có ai nộp đơn thách đấu!")
            else:
                lucky_challenger_id = random.choice(challengers)
                if channel:
                    embed = discord.Embed(
                        title=f"⚔️ KẾT QUẢ BỐC THĂM THÁCH ĐẤU TẦNG {tang_str} ⚔️",
                        description=f"Hệ thống đã chọn ngẫu nhiên người thách đấu may mắn từ `{len(challengers)}` ứng viên!",
                        color=0xe74c3c
                    )
                    embed.add_field(name="👑 Quản Tháp", value=owner_mention, inline=True)
                    embed.add_field(name="🗡️ Người Thách Đấu", value=f"<@{lucky_challenger_id}>", inline=True)
                    embed.add_field(name="🕒 Khung giờ Quản Tháp", value=f"`{session['owner_time']}`", inline=False)
                    embed.add_field(name="🕒 Khung giờ Thách Đấu", value=f"`{session['challenger_time']}`", inline=False)
                    embed.add_field(name="📜 Quy chế Teambuilding", value=get_teambuilding_text(), inline=False)
                    
                    await channel.send(content=f"🔔 Chúc mừng <@{lucky_challenger_id}> trúng suất thi đấu với Quản tháp {owner_mention}!", embed=embed)
            
            del data["active_registrations"][tang_str]
            updated = True
            
    if updated:
        save_data(data)

@bot.event
async def on_ready():
    print(f"Bot {bot.user.name} đã sẵn sàng vận hành!")
    if not reward_stardust_loop.is_running():
        reward_stardust_loop.start()
    if not check_challenge_cloisters.is_running():
        check_challenge_cloisters.start()
        
    try:
        synced = await bot.tree.sync()
        print(f"🎉 Đã đồng bộ thành công {len(synced)} lệnh Slash Commands.")
    except Exception as e:
        print(f"❌ Lỗi đồng bộ lệnh: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            f"🚫 Lệnh này chỉ được sử dụng tại kênh: <#{GYM_CHANNEL_ID}>!", 
            ephemeral=True
        )

# --- CÁC LỆNH SLASH COMMANDS ---

@bot.tree.command(name="mo_thach_dau", description="Bắt đầu nhận lời thách đấu cho một tầng trong vòng 12 tiếng.")
@is_gym_channel()
async def mo_thach_dau(interaction: discord.Interaction, tang: int, khung_gio_quan_thap: str, khung_gio_nguoi_dau: str):
    if interaction.user.id != YOUR_DISCORD_ID and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng lệnh đặc biệt này!", ephemeral=True)
        return

    valid_towers = str("123")
    if str(tang) not in valid_towers:
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

@bot.tree.command(name="thach_dau", description="Ghi danh đăng ký suất thách đấu may mắn.")
@is_gym_channel()
async def thach_dau(interaction: discord.Interaction, tang: int):
    valid_towers = str("123")
    if str(tang) not in valid_towers:
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

