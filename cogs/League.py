"""Giải đấu hàng tuần tranh chức Gym Leader.

Luồng một tuần:
  Thứ 2 00:00  mở đăng ký (im lặng) + nút Đăng ký
  CN   00:00   bốc thăm (im lặng)
  CN   06:00   thông báo lại (im lặng)
  CN   18:00   ping nhắc
  CN   23:59   hạn chót nhận kết quả

Toàn bộ luật nằm trong `match_logic.py`; cog này chỉ lo Discord.
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import match_logic as ml
from cogs.core import (
    GYM_CHANNEL_ID,
    fail_deferred,
    is_gym_channel,
    is_owner,
    read_data,
    update_data,
)

log = logging.getLogger(__name__)

CLICK_COOLDOWN = 3.0
_last_click = {}


def _click_allowed(user_id):
    moment = time.monotonic()
    if len(_last_click) > 5000:
        for uid, at in list(_last_click.items()):
            if moment - at > 60:
                _last_click.pop(uid, None)
    last = _last_click.get(user_id)
    if last is not None and moment - last < CLICK_COOLDOWN:
        return False
    _last_click[user_id] = moment
    return True


async def get_channel(client, channel_id):
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


def _registration_embed(state, current):
    week = state["week"]
    embed = discord.Embed(
        title=f"🏟️ TRANH CHỨC GYM LEADER — TUẦN {week['week_id']}",
        color=0x5865F2,
        description=(
            "Đăng ký mở **cả tuần**, đóng lúc **00:00 Chủ Nhật**.\n"
            "Bot bốc thăm **2 người** → đánh **BO1**.\n"
            "Người thắng thách đấu **Gym Leader hiện tại** trong trận **BO3**."
        ),
    )
    registrations = week["registrations"]
    if registrations:
        shown = ", ".join(f"<@{uid}>" for uid in registrations[:15])
        if len(registrations) > 15:
            shown += f" … (+{len(registrations) - 15})"
    else:
        shown = "*Chưa có ai*"
    embed.add_field(name="👑 Gym Leader", value=ml.leader_term_text(state), inline=False)
    embed.add_field(name=f"👥 Đã đăng ký ({len(registrations)})", value=shown, inline=False)
    embed.add_field(
        name="🎲 Bốc thăm",
        value=f"<t:{int(ml.draw_at(current).timestamp())}:F> (<t:{int(ml.draw_at(current).timestamp())}:R>)",
        inline=False,
    )
    embed.set_footer(text="Bấm nút bên dưới để đăng ký hoặc hủy đăng ký.")
    return embed


_REFRESH_AT = {}


async def refresh_registration(client):
    """Sửa lại tin đăng ký để danh sách luôn đúng (tối đa 1 lần mỗi 3 giây)."""
    data = await read_data(fresh=True)
    week = data["week"]
    message_id = week.get("message_id")
    if not message_id or week["phase"] != "open":
        return
    moment = time.monotonic()
    if moment - _REFRESH_AT.get(int(message_id), 0.0) < 3.0:
        return
    if len(_REFRESH_AT) > 200:
        _REFRESH_AT.clear()
    _REFRESH_AT[int(message_id)] = moment
    channel = await get_channel(client, GYM_CHANNEL_ID)
    if channel is None:
        return
    try:
        await channel.get_partial_message(int(message_id)).edit(
            embed=_registration_embed(data, ml.now())
        )
    except (discord.NotFound, discord.HTTPException):
        pass


def _draw_embed(state, current, kind, payload):
    week = state["week"]
    embed = discord.Embed(title=f"🎲 KẾT QUẢ BỐC THĂM — TUẦN {week['week_id']}", color=0xE67E22)
    if kind == "pair":
        a, b = payload["players"]
        embed.description = (
            f"**<@{a}> vs <@{b}>**\n"
            "Trận **BO1**. Hai bạn tự thương lượng thời gian thi đấu.\n"
            "Người thắng sẽ thách đấu Gym Leader trong trận **BO3**."
        )
    elif kind == "single":
        embed.description = (
            f"Chỉ có **<@{payload['challenger_id']}>** đăng ký.\n"
            f"Thách đấu trực tiếp Gym Leader <@{payload['leader_id']}> trong trận **BO3**."
        )
    elif kind == "crowned":
        embed.description = f"Chưa có Gym Leader — **<@{payload['user_id']}>** nhận chức tuần này."
    elif kind == "empty":
        embed.description = "Không ai đăng ký. Gym Leader giữ chức và được gia hạn **1 tuần**."
        embed.color = 0x95A5A6
    embed.add_field(
        name="⏰ Hạn chót",
        value=f"<t:{int(ml.deadline_at(current).timestamp())}:F> (<t:{int(ml.deadline_at(current).timestamp())}:R>)",
        inline=False,
    )
    return embed


def _match_mentions(state):
    """Mention những người đang có trận, để tin ping thực sự kéo họ vào."""
    week = state["week"]
    if week["phase"] == "qualifier":
        players = (week.get("qualifier") or {}).get("players", [])
        return " vs ".join(f"<@{uid}>" for uid in players)
    if week["phase"] == "title":
        title = week.get("title") or {}
        if title.get("challenger_id") and title.get("leader_id"):
            return f"<@{title['challenger_id']}> vs <@{title['leader_id']}>"
    return ""


class RegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Đăng ký / Hủy đăng ký",
        style=discord.ButtonStyle.success,
        custom_id="league:register",
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _click_allowed(interaction.user.id):
            return await interaction.response.send_message(
                "Bạn bấm hơi nhanh, vui lòng thử lại sau vài giây.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        status, payload = await update_data(
            lambda d: ml.toggle_registration(d, interaction.user.id, ml.now())
        )
        if status == "closed":
            text = "Đăng ký tuần này đã đóng."
        elif status == "leader":
            text = "Gym Leader không đăng ký tranh chức của chính mình."
        elif status == "left":
            text = f"Đã hủy đăng ký. Còn {payload['count']} người."
        else:
            text = f"Đã đăng ký. Hiện có {payload['count']} người."
        await interaction.followup.send(text, ephemeral=True)
        await refresh_registration(interaction.client)


class League(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(RegisterView())
        self.weekly_loop.start()

    def cog_unload(self):
        self.weekly_loop.cancel()

    # ------------------------------------------------------------------ #
    # Vòng lặp tuần
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=2)
    async def weekly_loop(self):
        try:
            actions = await update_data(lambda d: ml.ensure_week(d, ml.now()))
            if actions:
                await self._handle_actions(actions)
        except Exception:
            log.exception("Lỗi trong vòng lặp giải đấu tuần")

    @weekly_loop.before_loop
    async def before_weekly_loop(self):
        await self.bot.wait_until_ready()

    async def _handle_actions(self, actions):
        channel = await get_channel(self.bot, GYM_CHANNEL_ID)
        if channel is None:
            log.warning("Không tìm thấy kênh Gym %s", GYM_CHANNEL_ID)
            return
        for kind, payload in actions:
            try:
                await self._dispatch(channel, kind, payload)
            except discord.HTTPException:
                log.exception("Không gửi được thông báo giải đấu (%s)", kind)

    async def _dispatch(self, channel, kind, payload):
        current = ml.now()
        data = await read_data(fresh=True)

        if kind == "open":
            previous = payload.get("previous_message_id")
            if previous:
                # Tin đăng ký của tuần trước phải mất nút, tránh đăng ký nhầm tuần.
                await self._close_registration(channel, previous)
            message = await channel.send(
                embed=_registration_embed(data, current), view=RegisterView(), silent=True
            )
            await update_data(lambda d: d["week"].__setitem__("message_id", message.id))
            return

        if kind == "draw_pair":
            await self._close_registration(channel)
            await channel.send(embed=_draw_embed(data, current, "pair", payload), silent=True)
            return

        if kind == "draw_single":
            await self._close_registration(channel)
            await channel.send(embed=_draw_embed(data, current, "single", payload), silent=True)
            return

        if kind == "draw_empty":
            await self._close_registration(channel)
            await channel.send(embed=_draw_embed(data, current, "empty", payload), silent=True)
            return

        if kind == "draw_crowned":
            await self._close_registration(channel)
            await channel.send(embed=_draw_embed(data, current, "crowned", payload))
            return

        if kind == "notice":
            await channel.send(
                content=f"⏰ Nhắc lại trận tranh chức Gym Leader tuần **{payload['week_id']}**.",
                embed=discord.Embed(description=ml.week_summary(data, current), color=0x3498DB),
                silent=True,
            )
            return

        if kind == "ping":
            mentions = _match_mentions(data)
            prefix = f"{mentions} — " if mentions else ""
            await channel.send(
                content=f"🔔 {prefix}Còn vài giờ để chốt kết quả tuần **{payload['week_id']}**.",
                embed=discord.Embed(description=ml.week_summary(data, current), color=0xE74C3C),
            )
            return

        if kind == "timeout":
            await channel.send(
                "⏰ Hết hạn nhận kết quả tuần này. Gym Leader giữ chức và được gia hạn **1 tuần**.",
                silent=True,
            )
            return

        if kind == "missed":
            await channel.send(
                "⚠️ Bot không hoạt động vào Chủ Nhật nên tuần này không bốc thăm. "
                "Gym Leader giữ chức, tuần sau mở lại bình thường.",
                silent=True,
            )

    async def _close_registration(self, channel, message_id=None):
        if message_id is None:
            data = await read_data(fresh=True)
            message_id = data["week"].get("message_id")
        if not message_id:
            return
        try:
            partial = channel.get_partial_message(int(message_id))
            await partial.edit(
                content="🔒 Đã đóng đăng ký tuần này.",
                embed=discord.Embed(
                    description="Đăng ký đã đóng. Kết quả bốc thăm ở tin nhắn bên dưới.",
                    color=0x95A5A6,
                ),
                view=None,
            )
        except (discord.NotFound, discord.HTTPException):
            pass

    # ------------------------------------------------------------------ #
    # Lệnh người chơi
    # ------------------------------------------------------------------ #

    @app_commands.command(name="gym", description="Xem Gym Leader và tình trạng tuần này.")
    @is_gym_channel()
    async def gym(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await read_data()
        embed = discord.Embed(title="🏟️ GYM LEADER", color=0x2ECC71)
        embed.add_field(name="👑 Leader hiện tại", value=ml.leader_term_text(data), inline=False)
        embed.add_field(name="📅 Tuần này", value=ml.week_summary(data, ml.now()), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="dang_ky", description="Đăng ký hoặc hủy đăng ký tranh chức Gym Leader.")
    @is_gym_channel()
    async def dang_ky(self, interaction: discord.Interaction):
        if not _click_allowed(interaction.user.id):
            return await interaction.response.send_message(
                "Bạn bấm hơi nhanh, vui lòng thử lại sau vài giây.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        status, payload = await update_data(
            lambda d: ml.toggle_registration(d, interaction.user.id, ml.now())
        )
        if status == "closed":
            text = "Đăng ký tuần này đã đóng."
        elif status == "leader":
            text = "Gym Leader không đăng ký tranh chức của chính mình."
        elif status == "left":
            text = f"Đã hủy đăng ký. Còn {payload['count']} người."
        else:
            text = f"Đã đăng ký. Hiện có {payload['count']} người."
        await interaction.followup.send(text, ephemeral=True)
        await refresh_registration(interaction.client)

    @app_commands.command(name="lich_su", description="Xem lịch sử các trận gần đây.")
    @is_gym_channel()
    async def lich_su(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.defer()
        data = await read_data()
        history = data["history"]
        if member:
            history = [
                h for h in history
                if h.get("winner_id") == member.id or h.get("loser_id") == member.id
            ]
        history = history[-10:][::-1]
        embed = discord.Embed(title="📜 LỊCH SỬ TRẬN", color=0xE74C3C)
        if not history:
            embed.description = "*Chưa có trận nào.*"
        for item in history:
            label = {
                "qualifier": "BO1 vòng bốc thăm",
                "title": "BO3 tranh chức",
                "legacy": "Hệ thống cũ",
            }.get(item.get("type"), "Trận")
            embed.add_field(
                name=f"#{item.get('match_id')} — {label}",
                value=(
                    f"🏆 <@{item.get('winner_id')}> thắng <@{item.get('loser_id')}>\n"
                    f"📅 `{item.get('at', '—')}`"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="bang_xep_hang", description="Bảng xếp hạng thắng/thua và số lần làm Leader.")
    @is_gym_channel()
    async def bang_xep_hang(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await read_data()
        players = [
            (int(uid), p) for uid, p in data["players"].items() if str(uid).isdigit()
        ]
        wins = sorted(players, key=lambda x: x[1].get("wins", 0), reverse=True)[:5]
        titles = sorted(players, key=lambda x: x[1].get("titles", 0), reverse=True)[:5]

        def lines(items, key):
            if not items:
                return "*Chưa có dữ liệu*"
            return "\n".join(
                f"**{i}.** <@{uid}> — `{p.get(key, 0)}`" for i, (uid, p) in enumerate(items, 1)
            )

        embed = discord.Embed(title="🏆 BẢNG XẾP HẠNG", color=0xF1C40F)
        embed.add_field(name="⚔️ Nhiều trận thắng", value=lines(wins, "wins"), inline=False)
        embed.add_field(name="👑 Số lần làm Leader", value=lines(titles, "titles"), inline=False)
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Lệnh quản lý
    # ------------------------------------------------------------------ #

    @app_commands.command(name="ket_qua", description="[OWNER] Chốt người thắng của trận đang diễn ra.")
    @is_gym_channel()
    async def ket_qua(self, interaction: discord.Interaction, nguoi_thang: discord.Member):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer()
        data = await read_data()
        phase = data["week"]["phase"]
        current = ml.now()

        if phase == "qualifier":
            status, payload = await update_data(
                lambda d: ml.record_qualifier(d, nguoi_thang.id, current)
            )
        elif phase == "title":
            status, payload = await update_data(
                lambda d: ml.record_title(d, nguoi_thang.id, current)
            )
        else:
            return await fail_deferred(
                interaction, "Hiện không có trận nào đang chờ chốt kết quả."
            )

        if status == "not_in_match":
            return await fail_deferred(
                interaction, f"{nguoi_thang.mention} không có trong trận đang diễn ra."
            )
        if status == "phase":
            return await fail_deferred(interaction, "Trạng thái tuần đã thay đổi, thử lại sau.")

        embed = discord.Embed(title=f"✅ ĐÃ CHỐT — MATCH #{payload['match_id']}", color=0x2ECC71)
        if phase == "qualifier":
            if payload.get("crowned"):
                embed.description = f"👑 <@{payload['crowned']}> trở thành Gym Leader."
            else:
                embed.description = (
                    f"🏆 <@{payload['challenger_id']}> thắng BO1.\n"
                    f"Tiếp theo: thách đấu Gym Leader <@{payload['leader_id']}> — **BO3**."
                )
        else:
            if payload["leader_changed"]:
                embed.description = f"👑 <@{payload['winner_id']}> trở thành Gym Leader mới!"
            else:
                embed.description = (
                    f"🛡️ <@{payload['winner_id']}> giữ chức, gia hạn thêm 1 tuần."
                )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="gan_leader", description="[OWNER] Gán Gym Leader thủ công.")
    @is_gym_channel()
    async def gan_leader(self, interaction: discord.Interaction, member: discord.Member):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer()
        current = ml.now()

        def apply(data):
            start, end = ml.next_term(current)
            data["leader"] = {
                "user_id": member.id,
                "term_start": start.isoformat(),
                "term_end": end.isoformat(),
            }
            ml.get_player(data, member.id)["titles"] += 1
            return True

        await update_data(apply)
        await interaction.followup.send(
            f"👑 Đã gán {member.mention} làm Gym Leader cho tuần sau."
        )

    @app_commands.command(name="huy_tuan", description="[OWNER] Hủy tuần hiện tại và gia hạn Leader.")
    @is_gym_channel()
    async def huy_tuan(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer()
        current = ml.now()

        def apply(data):
            ml.extend_term(data, current)
            data["week"]["phase"] = "done"
            data["week"]["picked"] = []
            return True

        await update_data(apply)
        await interaction.followup.send("Đã hủy tuần này. Leader được gia hạn 1 tuần.")


async def setup(bot):
    await bot.add_cog(League(bot))
