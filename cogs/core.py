import copy
import json
import os

import discord
import psycopg
from psycopg.types.json import Jsonb
from discord.ext import commands, tasks
from discord import app_commands

OWNER_ID = 1031799680792809522
GYM_CHANNEL_ID = 1147411953501880390
DATA_KEY = "dark_gym_main"

DEFAULT_DATA = {
    "towers": {
        "1": {"title": "Antumbra Overlord", "user_id": None, "protected_until": None, "defense_streak": 0},
        "2": {"title": "Umbra Weaver", "user_id": None, "protected_until": None, "defense_streak": 0},
        "3": {"title": "Penumbra Sentinel", "user_id": None, "protected_until": None, "defense_streak": 0},
    },
    "wallets": {},
    "cooldowns": {},
    "active_registrations": {},
    "active_matches": {},
    "pending_challenges": {},
    "players": {},
    "match_history": [],
}


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


def _merge_defaults(data):
    """Add new schema fields without dropping existing saved game data."""
    merged = copy.deepcopy(DEFAULT_DATA)
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "towers" and isinstance(value, dict):
                for tier, info in value.items():
                    if tier in merged["towers"] and isinstance(info, dict):
                        merged["towers"][tier].update(info)
            else:
                merged[key] = value

    for tier, info in merged["towers"].items():
        info.setdefault("title", DEFAULT_DATA["towers"][tier]["title"])
        info.setdefault("user_id", None)
        info.setdefault("protected_until", None)
        info.setdefault("defense_streak", 0)

    for key in ("wallets", "cooldowns", "active_registrations", "active_matches", "pending_challenges", "players"):
        if not isinstance(merged.get(key), dict):
            merged[key] = {}
    if not isinstance(merged.get("match_history"), list):
        merged["match_history"] = []
    return merged


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
            (DATA_KEY, Jsonb(copy.deepcopy(DEFAULT_DATA))),
        )
    print("✅ PostgreSQL storage ready; clean state is used when the database is empty.")


def load_data():
    """Read saved state. Fail loudly on DB problems instead of silently losing ownership."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload FROM dark_gym_state WHERE state_key = %s",
            (DATA_KEY,),
        ).fetchone()
    if row is None:
        raise RuntimeError("Không tìm thấy dữ liệu Dark Gym trong PostgreSQL; từ chối tự tạo lại để tránh mất dữ liệu.")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise RuntimeError("Dữ liệu Dark Gym trong PostgreSQL không hợp lệ; từ chối ghi đè.")
    return _merge_defaults(data)


def save_data(data):
    """Atomically save the full game state to PostgreSQL."""
    if not isinstance(data, dict):
        raise TypeError("save_data expects a dictionary")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO dark_gym_state (state_key, payload, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (state_key)
            DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
            """,
            (DATA_KEY, Jsonb(data)),
        )


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_gym_channel():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.channel_id == GYM_CHANNEL_ID
    return app_commands.check(predicate)


def get_player(data, user_id):
    key = str(user_id)
    player = data["players"].setdefault(key, {
        "wins": 0,
        "losses": 0,
        "captures": 0,
        "defenses": 0,
        "win_streak": 0,
        "best_win_streak": 0,
        "towers_captured": [],
        "achievements": [],
    })
    player.setdefault("wins", 0)
    player.setdefault("losses", 0)
    player.setdefault("captures", 0)
    player.setdefault("defenses", 0)
    player.setdefault("win_streak", 0)
    player.setdefault("best_win_streak", 0)
    player.setdefault("towers_captured", [])
    player.setdefault("achievements", [])
    return player


def award_achievements(data, user_id):
    player = get_player(data, user_id)
    unlocked = []
    checks = [
        ("first_blood", "⚔️ First Blood", player["wins"] >= 1),
        ("conqueror", "👑 Conqueror", player["captures"] >= 1),
        ("guardian", "🛡️ Guardian", player["defenses"] >= 5),
        ("dark_master", "🌑 Dark Master", player["wins"] >= 20),
        ("unstoppable", "🔥 Unstoppable", player["best_win_streak"] >= 5),
        ("eclipse", "🌘 Eclipse", len(set(player["towers_captured"])) >= 3),
    ]
    for code, name, condition in checks:
        if condition and code not in player["achievements"]:
            player["achievements"].append(code)
            unlocked.append(name)
    return unlocked


def get_achievement_names(player):
    names = {
        "first_blood": "⚔️ First Blood",
        "conqueror": "👑 Conqueror",
        "guardian": "🛡️ Guardian",
        "dark_master": "🌑 Dark Master",
        "unstoppable": "🔥 Unstoppable",
        "eclipse": "🌘 Eclipse",
    }
    return [names[x] for x in player.get("achievements", []) if x in names]


def get_teambuilding_text():
    return (
        "⚠️ **LƯU Ý QUY ĐỊNH TEAMBUILDING:**\n"
        "• **Core Hệ Dark:** Cả Quản tháp & Người thách đấu bắt buộc mang tối thiểu **3/6 Pokémon** hệ Dark.\n"
        "• **Hybrid Team Sheet:**\n"
        "  ➔ Chỉ công khai (Move, Nature, Item, Gender) của **4/6 Pokémon** trong đội.\n"
        "  ➔ **2 Pokémon còn lại** sẽ được giấu kín hoàn toàn mọi thông tin trên."
    )


def format_remaining(target):
    seconds = max(0, int(target.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days} ngày {hours} giờ"
    if hours:
        return f"{hours} giờ {minutes} phút"
    return f"{minutes} phút"


class Core(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reward_stardust_loop.start()

    def cog_unload(self):
        self.reward_stardust_loop.cancel()

    @tasks.loop(hours=3)
    async def reward_stardust_loop(self):
        data = load_data()
        updated = False
        for info in data["towers"].values():
            user_id = info.get("user_id")
            if user_id:
                key = str(user_id)
                data["wallets"][key] = data["wallets"].get(key, 0) + 10
                updated = True
        if updated:
            save_data(data)

    @reward_stardust_loop.before_loop
    async def before_reward_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="stardust_altar", description="Xem số dư Dark Stardust.")
    @is_gym_channel()
    async def stardust_altar(self, interaction: discord.Interaction):
        data = load_data()
        balance = data["wallets"].get(str(interaction.user.id), 0)
        await interaction.response.send_message(
            f"🔮 **DARK STARDUST ALTAR**\n\n👤 {interaction.user.mention}\n✨ Số dư: **{balance} Dark Stardust**"
        )

    @app_commands.command(name="dark_rank", description="Xem bảng xếp hạng Dark Gym.")
    @is_gym_channel()
    async def dark_rank(self, interaction: discord.Interaction):
        data = load_data()
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
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="lich_su", description="Xem lịch sử các trận Dark Gym.")
    @is_gym_channel()
    async def lich_su(self, interaction: discord.Interaction, member: discord.User | None = None):
        data = load_data()
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
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="teambuilding", description="Xem quy định về cách xây dựng đội hình thi đấu.")
    @is_gym_channel()
    async def teambuilding(self, interaction: discord.Interaction):
        embed = discord.Embed(title="📝 QUY ĐỊNH TEAMBUILDING - THÁNH ĐỊA BÓNG ĐÊM", color=0x71368a)
        embed.description = get_teambuilding_text()
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Core(bot))
