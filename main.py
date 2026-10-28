import logging
import os
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

from cogs.core import GYM_CHANNEL_ID, initialize_storage

log = logging.getLogger(__name__)

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


# Bot chỉ dùng slash command nên không cần Message Content intent (đặc quyền).
intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        text = f"Lệnh này chỉ dùng được trong kênh <#{GYM_CHANNEL_ID}>."
    else:
        command = interaction.command.qualified_name if interaction.command else "?"
        log.error("Lỗi khi chạy lệnh /%s", command, exc_info=error)
        text = "Có lỗi xảy ra khi xử lý lệnh. Hãy thử lại sau."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


async def load_extensions():
    if not os.path.exists("./cogs"):
        raise RuntimeError("Không tìm thấy thư mục ./cogs.")
    for filename in sorted(os.listdir("./cogs")):
        if filename.endswith(".py"):
            try:
                await bot.load_extension(f"cogs.{filename[:-3]}")
                print(f"➔ Đã tải tính năng: {filename}")
            except Exception:
                # Extension failures should be visible in Render logs.
                log.exception("Không thể tải extension %s", filename)
                raise


@bot.event
async def on_ready():
    print(f"Bot {bot.user.name} đã sẵn sàng vận hành!")
    try:
        synced = await bot.tree.sync()
        print(f"🎉 Đã đồng bộ thành công {len(synced)} lệnh Slash Commands.")
    except Exception:
        log.exception("Lỗi đồng bộ slash commands")


async def main():
    discord.utils.setup_logging()
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
