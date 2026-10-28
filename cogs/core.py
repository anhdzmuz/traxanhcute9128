import asyncio
import copy
import json
import logging
import os

import discord
import psycopg
from psycopg.types.json import Jsonb
from discord.ext import commands, tasks
from discord import app_commands

import gym_logic as gl

log = logging.getLogger(__name__)

OWNER_ID = 1031799680792809522
GYM_CHANNEL_ID = 1147411953501880390
DATA_KEY = "dark_gym_main"


# --------------------------------------------------------------------------- #
# Lưu trữ PostgreSQL
# --------------------------------------------------------------------------- #

def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "Thiếu DATABASE_URL. Hãy cấu hình biến môi trường PostgreSQL trước khi chạy bot."
        )
    return url


def _connect():
    # PostgreSQL hosted externally should use TLS.
    return psycopg.connect(_database_url(), connect_timeout=10, sslmode="require")


def initialize_storage():
    """Create the PostgreSQL table and initialize a clean state only if empty.

    The old local gym_data.json is intentionally ignored. Existing database
    data is never reset during restarts or deployments.
    """
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dark_gym_state (
                state_key TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            INSERT INTO dark_gym_state (state_key, payload)
            VALUES (%s, %s)
            ON CONFLICT (state_key) DO NOTHING
            """,
            (DATA_KEY, Jsonb(copy.deepcopy(gl.DEFAULT_DATA))),
        )
    print("✅ PostgreSQL storage ready; clean state is used when the database is empty.")


def _decode_row(row):
    """Giải mã payload; báo lỗi rõ ràng thay vì âm thầm làm mất quyền sở hữu."""
    if row is None:
        raise RuntimeError("Không tìm thấy dữ liệu Dark Gym trong PostgreSQL; từ chối tự tạo lại để tránh mất dữ liệu.")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise RuntimeError("Dữ liệu Dark Gym trong PostgreSQL không hợp lệ; từ chối ghi đè.")
    return gl.merge_defaults(data)


def load_data():
    """Đọc trạng thái (chỉ đọc). Hàm đồng bộ — trong code async hãy dùng `read_data()`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload FROM dark_gym_state WHERE state_key = %s",
            (DATA_KEY,),
        ).fetchone()
    return _decode_row(row)


def _update_sync(fn):
    with _connect() as conn:
        # FOR UPDATE: các thao tác ghi khác phải chờ, nên không còn mất dữ liệu do ghi đè nhau.
        row = conn.execute(
            "SELECT payload FROM dark_gym_state WHERE state_key = %s FOR UPDATE",
            (DATA_KEY,),
        ).fetchone()
        data = _decode_row(row)
        before = copy.deepcopy(data)
        result = fn(data)
        if data != before:
            conn.execute(
                "UPDATE dark_gym_state SET payload = %s, updated_at = NOW() WHERE state_key = %s",
                (Jsonb(data), DATA_KEY),
            )
    return result


async def read_data():
    """Đọc trạng thái mà không chặn event loop."""
    return await asyncio.to_thread(load_data)


async def update_data(fn):
    """Đọc – sửa – ghi nguyên tử.

    `fn(data)` chạy trong một transaction có khóa dòng; nó sửa trực tiếp `data`
    và trả về kết quả bất kỳ. `fn` phải đồng bộ, nhanh và KHÔNG gọi Discord:
    hãy trả về thông tin cần thiết rồi gửi tin nhắn sau khi `update_data` kết thúc.
    Nếu `fn` ném lỗi, transaction được rollback và không có gì được ghi.
    """
    return await asyncio.to_thread(_update_sync, fn)


# --------------------------------------------------------------------------- #
# Tiện ích dùng chung cho các cog
# --------------------------------------------------------------------------- #

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_gym_channel():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.channel_id == GYM_CHANNEL_ID
    return app_commands.check(predicate)


async def fail_deferred(interaction: discord.Interaction, text: str):
    """Thay tin "đang suy nghĩ" công khai bằng một thông báo lỗi chỉ người dùng thấy."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
    await interaction.followup.send(text, ephemeral=True)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #

class Core(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.reward_stardust_loop.start()

    def cog_unload(self):
        self.reward_stardust_loop.cancel()

    @tasks.loop(minutes=5)
    async def reward_stardust_loop(self):
        # Một lỗi DB thoáng qua không được phép làm vòng lặp dừng hẳn.
        try:
            await update_data(lambda data: gl.apply_stardust(data, gl.now()))
        except Exception:
            log.exception("Lỗi khi cộng Dark Stardust định kỳ")

    @reward_stardust_loop.before_loop
    async def before_reward_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="stardust_altar", description="Xem số dư Dark Stardust.")
    @is_gym_channel()
    async def stardust_altar(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await read_data()
        balance = data["wallets"].get(str(interaction.user.id), 0)
        await interaction.followup.send(
            f"🔮 **DARK STARDUST ALTAR**\n\n👤 {interaction.user.mention}\n✨ Số dư: **{balance} Dark Stardust**"
        )

    @app_commands.command(name="dark_rank", description="Xem bảng xếp hạng Dark Gym.")
    @is_gym_channel()
    async def dark_rank(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await read_data()
        players = [(int(uid), player) for uid, player in data["players"].items()]
        wins = sorted(players, key=lambda x: (x[1].get("wins", 0), x[1].get("best_win_streak", 0)), reverse=True)[:5]
        captures = sorted(players, key=lambda x: x[1].get("captures", 0), reverse=True)[:5]
        defenses = sorted(players, key=lambda x: x[1].get("defenses", 0), reverse=True)[:5]

        def lines(items, key):
            if not items:
                return "*Chưa có dữ liệu*"
            return "\n".join(f"**{i}.** <@{uid}> — `{p.get(key, 0)}`" for i, (uid, p) in enumerate(items, 1))

        embed = discord.Embed(title="🌑 DARK GYM RANKING", color=0x2f3136)
        embed.add_field(name="🏆 Most Wins", value=lines(wins, "wins"), inline=False)
        embed.add_field(name="👑 Gym Captures", value=lines(captures, "captures"), inline=False)
        embed.add_field(name="🛡️ Best Defenders", value=lines(defenses, "defenses"), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="lich_su", description="Xem lịch sử các trận Dark Gym.")
    @is_gym_channel()
    async def lich_su(self, interaction: discord.Interaction, member: discord.User | None = None):
        await interaction.response.defer()
        data = await read_data()
        history = data["match_history"]
        if member:
            history = [m for m in history if m.get("challenger_id") == member.id or m.get("owner_id") == member.id]
        history = history[-10:][::-1]

        embed = discord.Embed(title="⚔️ DARK GYM — LỊCH SỬ TRẬN", color=0xe74c3c)
        if not history:
            embed.description = "*Chưa có trận đấu nào được ghi nhận.*"
        else:
            for match in history:
                result = "🏆 Challenger thắng" if match["result"] == "thang" else "🛡️ Quản Tháp phòng thủ thành công"
                embed.add_field(
                    name=f"#{match['match_id']} — Tầng {match['tower']}",
                    value=f"⚔️ <@{match['challenger_id']}> vs <@{match['owner_id']}>\n{result}\n📅 `{match['ended_at']}`",
                    inline=False,
                )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="teambuilding", description="Xem quy định về cách xây dựng đội hình thi đấu.")
    @is_gym_channel()
    async def teambuilding(self, interaction: discord.Interaction):
        embed = discord.Embed(title="📝 QUY ĐỊNH TEAMBUILDING - THÁNH ĐỊA BÓNG ĐÊM", color=0x71368a)
        embed.description = gl.get_teambuilding_text()
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Core(bot))
