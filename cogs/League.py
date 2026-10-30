"""Giải đấu hàng tuần tranh chức Gym Leader.

Luồng một tuần:
  Thứ 2 00:00  mở đăng ký (im lặng) + nút Đăng ký
  CN   00:00   bốc thăm (im lặng)
  CN   06:00   thông báo lại (im lặng)
  CN   18:00   ping nhắc
  CN   23:59   hạn chót nhận kết quả

Toàn bộ luật nằm trong `match_logic.py`; cog này chỉ lo Discord.
"""

import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import match_logic as ml
from cogs.core import (
    GYM_CHANNEL_ID,
    MATCH_CHANNEL_ID,
    OWNER_ID,
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
# (week_id, phase) -> lần cuối thử tạo chat riêng, tránh spam khi thiếu quyền.
_THREAD_RETRY = {}


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


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp")


def _is_proof_attachment(attachment):
    content_type = attachment.content_type or ""
    return content_type.startswith("image/") or attachment.filename.lower().endswith(IMAGE_SUFFIXES)


async def find_proof(channel, user_id, limit=60):
    """Ảnh minh chứng mới nhất của người chơi trong chat riêng."""
    try:
        async for message in channel.history(limit=limit):
            if message.author.id != user_id or not message.attachments:
                continue
            attachment = next(
                (a for a in message.attachments if _is_proof_attachment(a)), None
            )
            if attachment is None:
                continue
            return {
                "message_id": message.id,
                "channel_id": channel.id,
                "url": message.jump_url,
                "filename": attachment.filename,
            }
    except discord.HTTPException:
        log.warning("Không đọc được lịch sử chat để tìm ảnh minh chứng")
    return None


def _thread_panel(state, phase):
    best_of = "BO1" if phase == "qualifier" else "BO3"
    players = ml.match_players(state)
    embed = discord.Embed(
        title=f"⚔️ {best_of} — TUẦN {state['week']['week_id']}",
        color=0x5865F2,
        description=(
            "**" + " vs ".join(f"<@{uid}>" for uid in players) + "**\n\n"
            "1️⃣ Đánh xong, gửi **ảnh chụp màn hình** kết quả vào chat này.\n"
            "2️⃣ Một người bấm **📤 Nộp kết quả** và nhập tỷ số theo góc nhìn của mình.\n"
            "3️⃣ Người còn lại bấm **✅ Xác nhận** (hoặc **❌ Không đúng**).\n\n"
            f"Tỷ số hợp lệ: {'`1-0` / `0-1`' if phase == 'qualifier' else '`2-0` / `2-1` / `0-2` / `1-2`'}"
        ),
    )
    embed.add_field(
        name="⏰ Hạn chót",
        value=f"<t:{int(ml.deadline_at(ml.now()).timestamp())}:F>",
        inline=False,
    )
    embed.set_footer(text="Chat này sẽ bị xóa sau khi kết quả được xác nhận.")
    return embed


def _submit_view():
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="📤 Nộp kết quả",
            style=discord.ButtonStyle.primary,
            custom_id="league:submit",
        )
    )
    return view


def _confirm_view(phase, submitter_id):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="✅ Xác nhận",
            style=discord.ButtonStyle.success,
            custom_id=f"league:confirm:{phase}:{submitter_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="❌ Không đúng",
            style=discord.ButtonStyle.danger,
            custom_id=f"league:dispute:{phase}:{submitter_id}",
        )
    )
    return view


class ScoreModal(discord.ui.Modal):
    def __init__(self, phase):
        super().__init__(title="Nộp kết quả trận", timeout=300)
        self.phase = phase
        best_of = "BO1" if phase == "qualifier" else "BO3"
        self.score = discord.ui.TextInput(
            label=f"Tỷ số {best_of} (theo góc nhìn của bạn)",
            placeholder="1-0" if phase == "qualifier" else "2-1",
            max_length=7,
        )
        self.note = discord.ui.TextInput(
            label="Ghi chú (không bắt buộc)",
            required=False,
            max_length=200,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.score)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await read_data(fresh=True)
        if data["week"]["phase"] != self.phase:
            return await interaction.followup.send(
                "Trận này đã kết thúc hoặc đổi trạng thái.", ephemeral=True
            )
        players = ml.match_players(data)
        if interaction.user.id not in players:
            return await interaction.followup.send(
                "Bạn không có trong trận này.", ephemeral=True
            )

        best_of = 1 if self.phase == "qualifier" else 3
        if ml.parse_score(str(self.score), best_of) is None:
            rule = "1-0 hoặc 0-1" if best_of == 1 else "2-0, 2-1, 0-2 hoặc 1-2"
            return await interaction.followup.send(
                f"Tỷ số không hợp lệ. Nhập theo dạng `{rule}`.", ephemeral=True
            )

        proof = await find_proof(interaction.channel, interaction.user.id)
        if proof is None:
            return await interaction.followup.send(
                "Chưa thấy ảnh kết quả của bạn trong chat. "
                "Hãy gửi ảnh chụp màn hình rồi bấm nộp lại.",
                ephemeral=True,
            )

        status, payload = await update_data(
            lambda d: ml.submit_result(
                d, interaction.user.id, str(self.score), proof, ml.now()
            )
        )
        if status == "bad_score":
            return await interaction.followup.send(
                "Tỷ số không hợp lệ cho trận này.", ephemeral=True
            )
        if status != "ok":
            return await interaction.followup.send(
                "Không nộp được kết quả, thử lại sau.", ephemeral=True
            )

        note = str(self.note).strip()
        embed = discord.Embed(
            title="📤 BIÊN BẢN KẾT QUẢ",
            color=0xF1C40F,
            description=(
                f"<@{payload['submitter_id']}> nộp: **{payload['score']}** "
                f"→ người thắng: <@{payload['winner_id']}>\n"
                f"🖼️ Ảnh minh chứng: {proof['url']}"
                + (f"\n📝 {note}" if note else "")
            ),
        )
        embed.set_footer(text="Người còn lại bấm Xác nhận, hoặc Không đúng để chuyển owner.")
        await interaction.channel.send(
            content=f"<@{payload['opponent_id']}> xác nhận giúp kết quả này.",
            embed=embed,
            view=_confirm_view(self.phase, payload["submitter_id"]),
        )
        await interaction.followup.send(
            "Đã ghi biên bản, chờ đối thủ xác nhận.", ephemeral=True
        )


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
            # Bot khởi động lại giữa trận: tạo lại chat riêng nếu bị mất.
            data = await read_data(fresh=True)
            phase = data["week"]["phase"]
            if phase in ("qualifier", "title") and not (data["week"].get("threads") or {}).get(phase):
                await self._ensure_thread(phase)
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
            thread = await self._ensure_thread("qualifier")
            embed = _draw_embed(data, current, "pair", payload)
            if thread is not None:
                embed.add_field(name="💬 Chat riêng của trận", value=thread.mention, inline=False)
            await channel.send(embed=embed, silent=True)
            return

        if kind == "draw_single":
            await self._close_registration(channel)
            thread = await self._ensure_thread("title")
            embed = _draw_embed(data, current, "single", payload)
            if thread is not None:
                embed.add_field(name="💬 Chat riêng của trận", value=thread.mention, inline=False)
            await channel.send(embed=embed, silent=True)
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
            for thread_id in payload.get("threads") or []:
                await self._delete_thread(thread_id)
            return

        if kind == "missed":
            await channel.send(
                "⚠️ Bot không hoạt động vào Chủ Nhật nên tuần này không bốc thăm. "
                "Gym Leader giữ chức, tuần sau mở lại bình thường.",
                silent=True,
            )
            for thread_id in payload.get("threads") or []:
                await self._delete_thread(thread_id)

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
    # Chat riêng của trận
    # ------------------------------------------------------------------ #

    async def _ensure_thread(self, phase):
        """Tạo chat riêng cho trận nếu chưa có. Trả về Thread hoặc None."""
        data = await read_data(fresh=True)
        week = data["week"]
        if week["phase"] != phase:
            return None
        thread_id = (week.get("threads") or {}).get(phase)
        if thread_id:
            return await get_channel(self.bot, int(thread_id))

        key = (week["week_id"], phase)
        moment = time.monotonic()
        if moment - _THREAD_RETRY.get(key, 0.0) < 600:
            return None
        if len(_THREAD_RETRY) > 100:
            _THREAD_RETRY.clear()
        _THREAD_RETRY[key] = moment

        parent = await get_channel(self.bot, MATCH_CHANNEL_ID)
        if not isinstance(parent, discord.TextChannel):
            log.warning("Kênh trận đấu %s không phải text channel", MATCH_CHANNEL_ID)
            return None

        guild = parent.guild
        players = ml.match_players(data)
        members = []
        for user_id in players:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    member = None
            if member is not None:
                members.append(member)

        best_of = "BO1" if phase == "qualifier" else "BO3"
        names = " vs ".join(member.display_name[:14] for member in members) or "Trận đấu"
        try:
            thread = await parent.create_thread(
                name=f"⚔️ {week['week_id']} · {best_of} · {names}"[:95],
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
            )
        except discord.HTTPException:
            log.exception("Không tạo được chat riêng cho trận %s", phase)
            return None

        invited = list(members)
        owner = guild.get_member(OWNER_ID)
        if owner is None:
            try:
                owner = await guild.fetch_member(OWNER_ID)
            except discord.HTTPException:
                owner = None
        if owner is not None and owner not in invited:
            invited.append(owner)
        for member in invited:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                log.warning("Không thêm được %s vào chat riêng", member.id)

        await thread.send(embed=_thread_panel(data, phase), view=_submit_view())
        await update_data(
            lambda d: d["week"].setdefault("threads", {}).__setitem__(phase, thread.id)
        )
        return thread

    async def _delete_thread(self, thread_id):
        try:
            thread = await get_channel(self.bot, int(thread_id))
        except (TypeError, ValueError):
            return
        if not isinstance(thread, discord.Thread):
            # Chỉ xóa thread; tuyệt đối không đụng vào kênh thường.
            return
        try:
            await thread.delete()
        except discord.HTTPException:
            log.warning("Không xóa được chat riêng %s", thread_id)

    # ------------------------------------------------------------------ #
    # Nút trong chat riêng (custom_id động nên xử lý ở on_interaction)
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id") or ""
        try:
            if custom_id == "league:submit":
                return await self._on_submit(interaction)
            if custom_id.startswith("league:confirm:"):
                _, _, phase, submitter = custom_id.split(":")
                return await self._on_confirm(interaction, phase, int(submitter))
            if custom_id.startswith("league:dispute:"):
                _, _, phase, submitter = custom_id.split(":")
                return await self._on_dispute(interaction, phase, int(submitter))
        except Exception:
            log.exception("Lỗi khi xử lý nút %s", custom_id)
            text = "Có lỗi xảy ra, thử lại sau."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                pass

    async def _on_submit(self, interaction):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message(
                "Nút này chỉ dùng trong chat riêng của trận.", ephemeral=True
            )
        data = await read_data(fresh=True)
        if data["week"]["phase"] not in ("qualifier", "title"):
            return await interaction.response.send_message(
                "Hiện không có trận nào đang diễn ra.", ephemeral=True
            )
        if interaction.user.id not in ml.match_players(data):
            return await interaction.response.send_message(
                "Bạn không có trong trận này.", ephemeral=True
            )
        await interaction.response.send_modal(ScoreModal(data["week"]["phase"]))

    async def _on_confirm(self, interaction, phase, submitter_id):
        await interaction.response.defer(thinking=True)
        data = await read_data(fresh=True)
        if data["week"]["phase"] != phase:
            return await interaction.followup.send(
                "Trận này đã kết thúc hoặc đổi trạng thái.", ephemeral=True
            )
        submission = ml.get_submission(data, phase, submitter_id)
        if not isinstance(submission, dict):
            return await interaction.followup.send(
                "Không tìm thấy biên bản này.", ephemeral=True
            )
        players = ml.match_players(data)
        opponent = next((uid for uid in players if uid != submitter_id), None)
        if interaction.user.id not in (opponent, OWNER_ID):
            return await interaction.followup.send(
                "Chỉ đối thủ của người nộp (hoặc owner) mới xác nhận được.", ephemeral=True
            )
        if submission.get("status") == "confirmed":
            return await interaction.followup.send(
                "Biên bản này đã được xác nhận rồi.", ephemeral=True
            )

        status, payload = await update_data(
            lambda d: ml.confirm_result(d, submitter_id, ml.now())
        )
        if status == "already":
            return await interaction.followup.send(
                "Biên bản này đã được xác nhận rồi.", ephemeral=True
            )
        if status != "ok":
            return await interaction.followup.send(
                "Không xác nhận được, trạng thái tuần đã thay đổi.", ephemeral=True
            )

        await self._announce_result(payload)
        try:
            await interaction.followup.send("✅ Đã xác nhận kết quả.")
        except discord.HTTPException:
            pass

        if payload["phase"] == "qualifier" and not payload["result"].get("crowned"):
            await self._ensure_thread("title")
        await self._delete_thread(interaction.channel.id)

    async def _on_dispute(self, interaction, phase, submitter_id):
        await interaction.response.defer(thinking=True)
        data = await read_data(fresh=True)
        if data["week"]["phase"] != phase:
            return await interaction.followup.send(
                "Trận này đã kết thúc hoặc đổi trạng thái.", ephemeral=True
            )
        players = ml.match_players(data)
        opponent = next((uid for uid in players if uid != submitter_id), None)
        if interaction.user.id not in (opponent, OWNER_ID):
            return await interaction.followup.send(
                "Chỉ đối thủ của người nộp (hoặc owner) mới báo sai được.", ephemeral=True
            )
        status, payload = await update_data(
            lambda d: ml.dispute_result(d, submitter_id, interaction.user.id, ml.now())
        )
        if status != "ok":
            return await interaction.followup.send(
                "Không ghi nhận được, thử lại sau.", ephemeral=True
            )
        await interaction.followup.send(
            f"⚠️ <@{OWNER_ID}> — <@{submitter_id}> nộp `{payload['score']}` "
            f"nhưng <@{interaction.user.id}> nói không đúng.\n"
            "Owner dùng `/ket_qua` để chốt, hoặc bấm ✅ trên biên bản để chấp nhận."
        )

    async def _announce_result(self, payload):
        channel = await get_channel(self.bot, GYM_CHANNEL_ID)
        if channel is None:
            return
        result = payload["result"]
        winner = payload["winner_id"]
        if payload["phase"] == "qualifier":
            title = "🏆 KẾT QUẢ BO1 — VÒNG BỐC THĂM"
            if result.get("crowned"):
                text = f"👑 <@{winner}> trở thành Gym Leader."
            else:
                text = (
                    f"<@{winner}> thắng BO1.\n"
                    f"Tiếp theo: thách đấu <@{result['leader_id']}> — **BO3**."
                )
        else:
            title = "👑 KẾT QUẢ BO3 — TRANH CHỨC GYM LEADER"
            if result.get("leader_changed"):
                text = f"👑 <@{winner}> trở thành Gym Leader mới!"
            else:
                text = f"🛡️ <@{winner}> giữ chức, gia hạn thêm 1 tuần."
        embed = discord.Embed(
            title=title,
            color=0x2ECC71,
            description=f"{text}\n📊 Tỷ số: `{payload.get('score')}`",
        )
        embed.set_footer(
            text=f"Biên bản do người chơi nộp, đã được đối thủ xác nhận."
        )
        file = await self._proof_file(payload.get("submission") or {})
        try:
            await channel.send(embed=embed, file=file)
        except discord.HTTPException:
            log.exception("Không đăng được kết quả trận")

    async def _proof_file(self, submission):
        proof = submission.get("proof") or {}
        if not proof.get("message_id") or not proof.get("channel_id"):
            return None
        try:
            thread = await get_channel(self.bot, int(proof["channel_id"]))
            if thread is None:
                return None
            message = await thread.fetch_message(int(proof["message_id"]))
            if not message.attachments:
                return None
            attachment = message.attachments[0]
            return discord.File(
                io.BytesIO(await attachment.read()), filename=attachment.filename
            )
        except (discord.HTTPException, TypeError, ValueError):
            log.warning("Không tải lại được ảnh minh chứng")
            return None

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

        thread_id = (data["week"].get("threads") or {}).get(phase)
        if thread_id:
            await self._delete_thread(thread_id)
        if phase == "qualifier" and not payload.get("crowned"):
            thread = await self._ensure_thread("title")
            if thread is not None:
                await interaction.followup.send(f"💬 Chat riêng của trận BO3: {thread.mention}")

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
        data = await read_data(fresh=True)
        thread_ids = [t for t in (data["week"].get("threads") or {}).values() if t]

        def apply(data):
            ml.extend_term(data, current)
            data["week"]["phase"] = "done"
            data["week"]["picked"] = []
            return True

        await update_data(apply)
        for thread_id in thread_ids:
            await self._delete_thread(thread_id)
        await interaction.followup.send("Đã hủy tuần này. Leader được gia hạn 1 tuần.")


async def setup(bot):
    await bot.add_cog(League(bot))
