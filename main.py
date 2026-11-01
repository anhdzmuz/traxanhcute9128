import asyncio
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

# Trạng thái thử đăng nhập lại, hiển thị ở `/` để biết bot đang kẹt vì lý do gì.
LOGIN_STATE = {"attempt": 0, "delay": 0, "error": "", "ip": ""}


@app.route("/")
def home():
    if not bot.is_ready():
        waiting = ""
        if LOGIN_STATE["attempt"]:
            waiting = (
                f" | đang thử đăng nhập lại (lần {LOGIN_STATE['attempt']},"
                f" chờ {LOGIN_STATE['delay']}s)"
            )
        if LOGIN_STATE["error"]:
            waiting += f" | lỗi cuối: {LOGIN_STATE['error']}"
        ip = f" | IP ra: {LOGIN_STATE['ip']}" if LOGIN_STATE["ip"] else ""
        return (
            f"Working.. | Bot chưa kết nối Discord{waiting}{ip}"
            f" | kênh Gym: {GYM_CHANNEL_ID}"
        )
    user = bot.user
    name = f"{user} ({user.id})" if user else "?"
    ip = f" | IP ra: {LOGIN_STATE['ip']}" if LOGIN_STATE["ip"] else ""
    return (
        f"Working.. | Bot online: {name} | {len(bot.guilds)} server "
        f"| ping {round(bot.latency * 1000)}ms{ip} | kênh Gym: {GYM_CHANNEL_ID}"
    )


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# Render free tắt service sau ~15 phút không có request HTTP, dù bot vẫn đang
# chạy. Tự gọi vào URL công khai mỗi 10 phút để giữ instance luôn thức.
# `RENDER_EXTERNAL_URL` do Render tự cấp, nên đổi tên/region service vẫn đúng.
# Đặt KEEP_ALIVE_URL rỗng để tắt; khi chạy local sẽ không tự ping.
KEEP_ALIVE_URL = (
    os.environ.get("KEEP_ALIVE_URL", "").strip()
    or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
)
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


def _detect_egress_ip():
    """Lấy IP công khai của service, để biết đã đổi được IP chưa."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=15) as resp:
            LOGIN_STATE["ip"] = resp.read(64).decode().strip()
    except Exception as exc:  # noqa: BLE001 - chỉ để hiển thị, không quan trọng
        log.warning("Không lấy được IP ra: %s", exc)


def keep_alive():
    Thread(target=run_web, daemon=True).start()
    Thread(target=_self_ping, daemon=True, name="self-ping").start()
    Thread(target=_detect_egress_ip, daemon=True, name="egress-ip").start()


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


async def start_bot(token: str) -> None:
    """Đăng nhập Discord, tự thử lại khi bị chặn tạm thời.

    Render free dùng IP dùng chung nên hay bị Cloudflare trả 429/1015
    ("You are being rate limited") ngay ở bước ``GET /users/@me``. Trước đây
    lỗi này làm cả process thoát, Render restart liên tục và càng bị chặn.
    Giữ Flask sống và thử lại với backoff 30s → 300s.
    """
    delay = 30
    while True:
        try:
            await bot.start(token)
            return
        except discord.LoginFailure:
            # Token sai thì thử lại cũng vô ích, phải báo ngay.
            raise
        except Exception as exc:  # noqa: BLE001 - mọi lỗi mạng/HTTP đều đáng thử lại
            LOGIN_STATE["attempt"] += 1
            LOGIN_STATE["delay"] = delay
            status = getattr(exc, "status", None)
            first_line = str(exc).splitlines()[0][:160]
            LOGIN_STATE["error"] = (
                f"{type(exc).__name__} {status}: {first_line}" if status else f"{type(exc).__name__}: {first_line}"
            )
            hint = ""
            if status == 429:
                hint = (
                    " (429 — IP đang bị Cloudflare tạm chặn,"
                    " không phải lỗi code)"
                )
            log.warning(
                "Không đăng nhập được Discord: %s: %s%s. Thử lại sau %ss.",
                type(exc).__name__,
                first_line,
                hint,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


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
        await start_bot(token)


if __name__ == "__main__":
    asyncio.run(main())
