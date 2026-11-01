import logging
import os
import time
import urllib.request
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

from cogs.core import GYM_CHANNEL_ID, initialize_storage, is_owner

log = logging.getLogger(__name__)

# Web endpoint used by Render health checks.
app = Flask(__name__)


@app.route("/")
def home():
    if not bot.is_ready():
        return f"Working.. | Bot chưa kết nối Discord | kênh Gym: {GYM_CHANNEL_ID}"
    user = bot.user
    name = f"{user} ({user.id})" if user else "?"
    return (
        f"Working.. | Bot online: {name} | {len(bot.guilds)} server "
        f"| ping {round(bot.latency * 1000)}ms | kênh Gym: {GYM_CHANNEL_ID}"
    )


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# Render free tắt service sau ~15 phút không có request HTTP, dù bot vẫn đang
# chạy. Tự gọi vào URL công khai mỗi 10 phút để giữ instance luôn thức.
# Đặt KEEP_ALIVE_URL rỗng để tắt; khi chạy local sẽ không tự ping.
KEEP_ALIVE_URL = os.environ.get("KEEP_ALIVE_URL", "").strip()
if not KEEP_ALIVE_URL and os.environ.get("RENDER"):
    KEEP_ALIVE_URL = "https://traxanhcute9128.onrender.com"
try:
    KEEP_ALIVE_INTERVAL = max(60, int(os.environ.get("KEEP_ALIVE_INTERVAL", "600")))
except ValueError:
    KEEP_ALIVE_INTERVAL = 600


def _self_ping():
    if not KEEP_ALIVE_URL:
        return
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            with urllib.request.urlopen(KEEP_ALIVE_URL, timeout=20) as resp:
                resp.read(64)
        except Exception as exc:  # noqa: BLE001 - lỗi mạng chỉ cần biết, không được chết
            log.warning("Self-ping %s lỗi: %s", KEEP_ALIVE_URL, exc)


def keep_alive():
    Thread(target=run_web, daemon=True).start()
    Thread(target=_self_ping, daemon=True, name="self-ping").start()


# Bot chỉ dùng slash command nên không cần Message Content intent (đặc quyền).
intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


async def log_interaction_delay(interaction: discord.Interaction) -> bool:
    """Ghi log khi interaction tới muộn — Discord báo "không phản hồi" sau 3 giây.

    Nếu log này xuất hiện nhiều nghĩa là mạng/gateway chậm (Render hoặc Discord),
    không phải lỗi xử lý lệnh.
    """
    delay = (discord.utils.utcnow() - interaction.created_at).total_seconds()
    command = getattr(interaction.command, "qualified_name", "?")
    if delay > 1.0:
        log.warning(
            "Interaction /%s đến muộn %.2fs (ping %.0fms)",
            command,
            delay,
            bot.latency * 1000,
        )
    else:
        log.info("Nhận /%s sau %.2fs (ping %.0fms)", command, delay, bot.latency * 1000)
    return True


# `CommandTree.interaction_check` là method cần gán, không phải decorator.
bot.tree.interaction_check = log_interaction_delay


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        text = f"Lệnh này chỉ dùng được trong kênh <#{GYM_CHANNEL_ID}>."
    else:
        command = interaction.command.qualified_name if interaction.command else "?"
        original = getattr(error, "original", error)
        log.error("Lỗi khi chạy lệnh /%s", command, exc_info=original)
        text = "Có lỗi xảy ra khi xử lý lệnh. Hãy thử lại sau."
        # Owner thấy nguyên nhân thật để báo lại, người chơi chỉ thấy thông báo chung.
        if is_owner(interaction.user.id):
            text += f"\n`{type(original).__name__}: {str(original)[:300]}`"
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
