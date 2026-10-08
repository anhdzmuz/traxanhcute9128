import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
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
    # Render yêu cầu chạy trên cổng được cấp qua biến môi trường PORT
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
GYM_CHANNEL_ID = 1234567890123456789  # Thay ID kênh của bạn vào đây

default_data = {
    "towers": {
        "1": {"title": "Antumbra Overlord", "user_id": None, "protected_until": None},
        "2": {"title": "Umbra Weaver", "user_id": None, "protected_until": None},
        "3": {"title": "Penumbra Sentinel", "user_id": None, "protected_until": None}
    },
    "wallets": {},
    "cooldowns": {}
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(default_data, f, indent=4)
        return default_data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def is_gym_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != GYM_CHANNEL_ID:
            await interaction.response.send_message(
                f"🚫 Lệnh này chỉ được sử dụng tại kênh: <#{GYM_CHANNEL_ID}>!", 
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

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

@bot.event
async def on_ready():
    print(f"Bot {bot.user.name} đã sẵn sàng vận hành!")
    try:
        synced = await bot.tree.sync()
        print(f"Đã đồng bộ {len(synced)} lệnh.")
    except Exception as e:
        print(e)
    if not reward_stardust_loop.is_running():
        reward_stardust_loop.start()

# --- CÁC LỆNH SLASH COMMANDS ---
@bot.tree.command(name="thap_gym", description="Xem danh sách Quản Tháp.")
@is_gym_channel()
async def thap_gym(interaction: discord.Interaction):
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

@bot.tree.command(name="thach_dau", description="Gửi lời thách đấu.")
@is_gym_channel()
async def thach_dau(interaction: discord.Interaction, tang: int):
    if tang not in:
        await interaction.response.send_message("Tầng không hợp lệ!", ephemeral=True)
        return
    data = load_data()
    user_id_str = str(interaction.user.id)
    now = datetime.now()
    cd_key = f"{user_id_str}_{tang}"
    if cd_key in data["cooldowns"]:
        cd_time = datetime.fromisoformat(data["cooldowns"][cd_key])
        if now < cd_time:
            remaining = cd_time - now
            hours, remainder = divmod(remaining.seconds, 3600)
            await interaction.response.send_message(f"❌ Bạn đang trong thời gian hồi chiêu phục thù! Còn {remaining.days} ngày {hours} giờ.", ephemeral=True)
            return
    tower_info = data["towers"][str(tang)]
    if not tower_info["user_id"]:
        await interaction.response.send_message("Tầng này hiện đang trống!", ephemeral=True)
        return
    if tower_info["protected_until"]:
        p_time = datetime.fromisoformat(tower_info["protected_until"])
        if now < p_time:
            await interaction.response.send_message(f"🛡️ Quản Tháp đang được bảo hộ đến: `{p_time.strftime('%H:%M - %d/%m/%Y')}`", ephemeral=True)
            return
    await interaction.response.send_message(f"⚔️ Trainer {interaction.user.mention} thách đấu Tầng {tang}: <@{tower_info['user_id']}>!")

@bot.tree.command(name="ket_qua_gym", description="[ADMIN] Cập nhật kết quả.")
@commands.has_permissions(administrator=True)
@is_gym_channel()
async def ket_qua_gym(interaction: discord.Interaction, tang: int, nguoi_thach_dau: discord.User, ket_qua: str):
    if tang not in: return
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
        await interaction.response.send_message(f"💀 Quản tháp bảo vệ ngôi thành công! {nguoi_thach_dau.mention} bị cấm phục thù 48 giờ.")

@bot.tree.command(name="stardust_altar", description="Xem số dư Dark Stardust.")
@is_gym_channel()
async def stardust_altar(interaction: discord.Interaction):
    data = load_data()
    balance = data["wallets"].get(str(interaction.user.id), 0)
    await interaction.response.send_message(f"🔮 Số dư của bạn: ✨ **{balance} Dark Stardust**")

# Kích hoạt Web Server trước rồi chạy Bot
keep_alive()

# ⚠️ LƯU Ý LỚN: Trên Render, KHÔNG NÊN dán token trực tiếp vào đây để bảo mật. 
# Ta sẽ dùng biến môi trường (Environment Variable) tên là DISCORD_TOKEN.
bot.run(os.environ.get("DISCORD_TOKEN"))
