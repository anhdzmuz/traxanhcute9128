import discord
from discord.ext import commands
import os
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

@bot.event
async def on_ready():
    print(f"Bot {bot.user.name} đã sẵn sàng vận hành!")
    
    # Quét thư mục cogs và nạp từng file tính năng độc lập
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                print(f"➔ Đã tải tính năng: {filename}")
            except Exception as e:
                print(f"❌ Lỗi tải file {filename}: {e}")
                
    try:
        synced = await bot.tree.sync()
        print(f"🎉 Đã đồng bộ thành công {len(synced)} lệnh Slash Commands.")
    except Exception as e:
        print(f"❌ Lỗi đồng bộ lệnh: {e}")

# Kích hoạt Web Server trước rồi chạy Bot
keep_alive()

BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
if not BOT_TOKEN:
    BOT_TOKEN = "abc"

bot.run(BOT_TOKEN)
