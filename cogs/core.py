import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os

DATA_FILE = "gym_data.json"
GYM_CHANNEL_ID = 1147411953501880390  

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
        "⚠️ **LƯU Ý QUY ĐỊNH VỀ TEAMBUILDING:**\n"
        "• **Core Hệ Dark:** Cả Quản tháp & Người thách đấu bắt buộc mang tối thiểu **3/6 Pokémon** hệ Dark.\n"
        "• **Hybrid Team Sheet:**
        "  ➔ Chỉ công khai (Move, Nature, Item, Gender) của **4/6 Pokémon** trong đội.\n"
        "  ➔ **2 Pokémon còn lại** sẽ được giấu kín hoàn toàn mọi thông tin trên."
    )

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
        for tier, info in data["towers"].items():
            user_id = info["user_id"]
            if user_id:
                user_id_str = str(user_id)
                data["wallets"][user_id_str] = data["wallets"].get(user_id_str, 0) + 10
                updated = True
        if updated:
            save_data(data)

    @app_commands.command(name="stardust_altar", description="Xem số dư Dark Stardust.")
    @is_gym_channel()
    async def stardust_altar(self, interaction: discord.Interaction):
        data = load_data()
        balance = data["wallets"].get(str(interaction.user.id), 0)
        await interaction.response.send_message(f"🔮 Số dư của bạn: ✨ **{balance} Dark Stardust**")

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                f"🚫 Lệnh này chỉ được sử dụng tại kênh: <#{GYM_CHANNEL_ID}>!", 
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(Core(bot))
