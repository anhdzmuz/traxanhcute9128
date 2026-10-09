import discord
from discord.ext import commands
import os
from flask import Flask
from threading import Thread

from cogs.core import initialize_storage

# Web endpoint used by Render health checks.
app = Flask(__name__)


@app.route("/")
def home():
    return "Thánh Địa Bóng Đêm đang hoạt động ổn định!"


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=run_web, daemon=True).start()


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)


async def load_extensions():
    if not os.path.exists("./cogs"):
        raise RuntimeError("Không tìm thấy thư mục ./cogs.")
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py"):
            try:
                await bot.load_extension(f"cogs.{filename[:-3]}")
                print(f"➔ Đã tải tính năng: {filename}")
            except Exception:
                # Extension failures should be visible in Render logs.
                import logging
                logging.exception("Không thể tải extension %s", filename)
                raise


@bot.event
async def on_ready():
    print(f"Bot {bot.user.name} đã sẵn sàng vận hành!")
    try:
        synced = await bot.tree.sync()
        print(f"🎉 Đã đồng bộ thành công {len(synced)} lệnh Slash Commands.")
    except Exception:
        import logging
        logging.exception("Lỗi đồng bộ slash commands")


async def main():
    keep_alive()
    # Validate DATABASE_URL and initialize the clean state before loading cogs.
    initialize_storage()
    async with bot:
        await load_extensions()
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("Thiếu DISCORD_TOKEN trong Environment Variables.")
        await bot.start(token)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
