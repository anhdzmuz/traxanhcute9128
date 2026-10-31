"""Giải đấu hàng tuần tranh chức Gym Leader (Gym Dark TGA).

Mỗi giải chạy trong 1 tuần:

  Thứ 2 00:00   chốt đăng ký tuần trước, mở giải + panel WL mới
  Thứ 2 – Thứ 4 vòng loại BO1, mỗi người đấu 1 trận; ai thắng (hoặc lẻ cặp) đi tiếp
  Thứ 5 00:00   cặp nào chưa báo kết quả -> loại cả 2
  Thứ 5 – Thứ 7 ghép cặp lần 2, chốt danh sách random
  CN    00:00   random 2 Trainer vào chung kết
  CN            chung kết BO3
  Thứ 2 00:00   người thắng nhậm chức Gym Leader

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


# Quyền bot cần trong kênh Gym (không cần Administrator).
PANEL_PERMS = {
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "attach_files": "Attach Files",
    "read_message_history": "Read Message History",
}

THREAD_PERMS = {
    "create_private_threads": "Create Private Threads",
    "send_messages_in_threads": "Send Messages in Threads",
    "manage_threads": "Manage Threads",
    "read_message_history": "Read Message History",
    "attach_files": "Attach Files",
}


def missing_permissions(channel, required=None):
    """Tên các quyền bot còn thiếu trong kênh (rỗng nếu đủ)."""
    guild = getattr(channel, "guild", None)
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return []
    perms = channel.permissions_for(me)
    return [
        label
        for attr, label in (required or PANEL_PERMS).items()
        if not getattr(perms, attr, False)
    ]


# --------------------------------------------------------------------------- #
# Luật của Gym (hiển thị trong panel và /luat)
# --------------------------------------------------------------------------- #

RULES_TEAM = (
    "• Tối thiểu **3/6 Pokémon hệ Dark** trong team.\n"
    "• Chỉ dùng **1 team cố định** trong tuần đấu đó — không đổi Pokémon giữa các vòng.\n"
    "• **Hybrid team sheet:** bạn để lộ **2/6** Pokémon của mình.\n"
    "• Cơ chế cấm chọn: *Updating…*"
)

RULES_FORMAT = (
    "**Vòng loại (BO1)** — Thứ 2 → 00:00 Thứ 5\n"
    "• Ghép cặp ngẫu nhiên, mỗi người đấu **1 trận BO1**; ai thắng (hoặc lẻ cặp được đi tiếp) vào danh sách.\n"
    "• Không báo kết quả trước **00:00 Thứ 5** → **loại cả 2**.\n\n"
    "**Ghép cặp lần 2 (BO1)** — Thứ 5 → 00:00 Chủ Nhật\n"
    "• Những người còn lại được ghép cặp lại; ai vào được vòng này đều nằm trong danh sách random.\n"
    "• **00:00 Chủ Nhật** (hoặc sớm hơn khi mọi trận đã xong) bot random **2 Trainer** vào chung kết (ưu tiên cao → dễ được chọn hơn).\n"
    "• Ai vào vòng này mà không được chọn → **+1 ưu tiên** cho lần sau.\n\n"
    "**Chung kết (BO3)** — Chủ Nhật\n"
    "• 2 Trainer đấu nhau để giành slot Gym Leader **tuần tiếp theo**.\n"
    "• Người thắng nhậm chức lúc **00:00 Thứ 2**."
)

RULES_REWARD = (
    "• Lệ phí tham gia: **miễn phí** (tạm thời).\n"
    "• Lần đầu trở thành Gym Leader (thời vụ): được chọn **1 Pokémon Shiny** có trong kho của Gym.\n"
    "• *Đang cập nhật…*"
)

RULES_NOTE = (
    "• Bot tạo **chat riêng** cho từng cặp để hai bạn tự hẹn giờ.\n"
    "• Đánh xong: gửi **ảnh chụp màn hình** vào chat riêng, bấm **📤 Nộp kết quả**, "
    "đối thủ bấm **✅ Xác nhận**.\n"
    "• Tỷ số nhập theo góc nhìn của bạn: BO1 `1-0` / `0-1`, BO3 `2-0` / `2-1` / `0-2` / `1-2`.\n"
    "• Chat riêng tự xóa sau khi kết quả được xác nhận (ảnh minh chứng được đăng lại kèm kết quả)."
)


def rules_embeds():
    """Toàn bộ luật của Gym, dùng cho /luat."""
    intro = discord.Embed(
        title="🏟️ Binh Đoàn Bóng Tối ┋ Gym Dark TGA",
        color=0x2C3E50,
        description=(
            f"**Chủ Gym:** <@{OWNER_ID}>\n"
            "Giải đấu tuần: thắng chung kết BO3 là thành Gym Leader tuần sau."
        ),
    )
    intro.add_field(name="🧬 Team building", value=RULES_TEAM, inline=False)
    format_embed = discord.Embed(title="📜 Thể thức thi đấu", color=0x5865F2, description=RULES_FORMAT)
    format_embed.add_field(name="🎁 Phần thưởng", value=RULES_REWARD, inline=False)
    format_embed.add_field(name="🤝 Cách báo kết quả", value=RULES_NOTE, inline=False)
    format_embed.set_footer(text="Luật có thể cập nhật — bot sẽ thông báo khi thay đổi.")
    return [intro, format_embed]


# --------------------------------------------------------------------------- #
# Panel đăng ký
# --------------------------------------------------------------------------- #

def _registration_embed(state, current):
    signup = state["signup"]
    wl = signup.get("wl") or 1
    embed = discord.Embed(
        title=f"Đăng kí thách đấu Gym Leader - WL{wl}",
        color=0x5865F2,
        description=(
            "Đăng ký mở **cả tuần**, chốt lúc **00:00 Thứ 2** rồi bước vào giải.\n"
            "**Thứ 2 – Thứ 4:** vòng loại BO1 — mỗi người đấu 1 trận, ai thắng đi tiếp.\n"
            "**Thứ 5 – Thứ 7:** ghép cặp lần 2, rồi random 2 Trainer vào chung kết.\n"
            "**Chủ Nhật:** chung kết **BO3** — thắng là Gym Leader từ **00:00 Thứ 2** tuần sau.\n"
            "Xem đầy đủ luật bằng `/luat`."
        ),
    )
    registrations = signup["registrations"]
    if registrations:
        shown = ", ".join(f"<@{uid}>" for uid in registrations[:15])
        if len(registrations) > 15:
            shown += f" … (+{len(registrations) - 15})"
    else:
        shown = "*Chưa có ai*"
    embed.add_field(name="👑 Gym Leader", value=ml.leader_term_text(state), inline=False)
    embed.add_field(name=f"👥 Đã đăng ký ({len(registrations)})", value=shown, inline=False)
    embed.add_field(
        name="⏰ Chốt đăng ký",
        value=f"<t:{int(ml.signup_close_at(current).timestamp())}:F> (<t:{int(ml.signup_close_at(current).timestamp())}:R>)",
        inline=False,
    )
    embed.add_field(name="🧬 Team building", value=RULES_TEAM, inline=False)
    embed.set_footer(
        text="Bấm nút để đăng ký — bấm lại (hoặc nút Hủy) để hủy. Tin tự nhắc lại mỗi 8 giờ."
    )
    return embed


_REFRESH_AT = {}
# match_id -> lần cuối thử tạo chat riêng, tránh spam khi thiếu quyền.
_THREAD_RETRY = {}


async def refresh_registration(client):
    """Sửa lại tin đăng ký để danh sách luôn đúng (tối đa 1 lần mỗi 3 giây)."""
    data = await read_data(fresh=True)
    signup = data["signup"]
    message_id = signup.get("message_id")
    if not message_id or not signup.get("open"):
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


# --------------------------------------------------------------------------- #
# Embed thông báo
# --------------------------------------------------------------------------- #

def _draw_embed(kind, payload):
    """Embed cho kết quả ghép cặp / chốt danh sách."""
    wl = payload.get("wl") or 0
    if kind == "draw_qualifier":
        embed = discord.Embed(
            title=f"🎲 VÒNG LOẠI BO1 — WL{wl} · Vòng {payload['round']}",
            color=0xE67E22,
            description=(
                "Bot ghép cặp ngẫu nhiên. Hai bạn tự hẹn giờ trong **chat riêng**.\n"
                "**Không báo kết quả trước 00:00 Thứ 5 → loại cả 2.**"
            ),
        )
        for pair in payload["pairs"]:
            embed.add_field(name="⚔️ Cặp đấu", value=ml.match_line({"players": pair}), inline=False)
        if payload.get("byes"):
            embed.add_field(
                name="🎫 Đi tiếp (lẻ cặp)",
                value=", ".join(f"<@{uid}>" for uid in payload["byes"]),
                inline=False,
            )
    elif kind == "draw_second":
        embed = discord.Embed(
            title=f"🎲 GHÉP CẶP LẦN 2 — WL{wl} · Vòng {payload['round']}",
            color=0x9B59B6,
            description=(
                "Vòng loại đã khép lại. Bot ghép cặp lại để chốt danh sách random.\n"
                "Khi các trận này xong (chậm nhất **00:00 Chủ Nhật**), bot random "
                "**2 Trainer** vào chung kết."
            ),
        )
        for pair in payload["pairs"]:
            embed.add_field(name="⚔️ Cặp đấu", value=ml.match_line({"players": pair}), inline=False)
        if payload.get("byes"):
            embed.add_field(
                name="🎫 Đi tiếp (lẻ cặp)",
                value=", ".join(f"<@{uid}>" for uid in payload["byes"]),
                inline=False,
            )
    else:  # draw_final
        a, b = payload["players"]
        embed = discord.Embed(
            title=f"👑 CHUNG KẾT BO3 — WL{wl}",
            color=0xF1C40F,
            description=(
                f"**<@{a}> vs <@{b}>**\n"
                "Hai Trainer đấu **BO3** để giành slot Gym Leader tuần sau.\n"
                "Người thắng nhậm chức lúc **00:00 Thứ 2**."
            ),
        )
        skipped = [uid for uid in payload.get("pool", []) if uid not in payload["players"]]
        if skipped:
            embed.add_field(
                name="➕ +1 ưu tiên lần sau",
                value=", ".join(f"<@{uid}>" for uid in skipped),
                inline=False,
            )
    return embed


def _match_mentions(state):
    lines = [ml.match_line(match) for match in ml.open_matches(state)]
    return " · ".join(lines)


# --------------------------------------------------------------------------- #
# Ảnh minh chứng
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# View / modal
# --------------------------------------------------------------------------- #

def _thread_panel(state, match):
    best_of = "BO3" if match["best_of"] == 3 else "BO1"
    wl = state["tournament"].get("wl") or 0
    a, b = match["players"]
    scores = "`1-0` / `0-1`" if best_of == "BO1" else "`2-0` / `2-1` / `0-2` / `1-2`"
    embed = discord.Embed(
        title=f"⚔️ {best_of} — WL{wl} · Trận #{match['id']}",
        color=0x5865F2,
        description=(
            f"**<@{a}> vs <@{b}>**\n\n"
            "1️⃣ Hai bạn tự hẹn giờ trong chat này.\n"
            "2️⃣ Đánh xong, gửi **ảnh chụp màn hình** kết quả.\n"
            "3️⃣ Một người bấm **📤 Nộp kết quả** và nhập tỷ số theo góc nhìn của mình.\n"
            "4️⃣ Người còn lại bấm **✅ Xác nhận** (hoặc **❌ Không đúng**).\n\n"
            f"Tỷ số hợp lệ: {scores}"
        ),
    )
    deadline = ml.tournament_deadline(state["tournament"])
    if deadline:
        embed.add_field(
            name="⏰ Hạn chót",
            value=f"<t:{int(deadline.timestamp())}:F> (<t:{int(deadline.timestamp())}:R>)",
            inline=False,
        )
    embed.set_footer(text="Chat này sẽ bị xóa sau khi kết quả được xác nhận.")
    return embed


def _submit_view(match_id):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="📤 Nộp kết quả",
            style=discord.ButtonStyle.primary,
            custom_id=f"league:submit:{match_id}",
        )
    )
    return view


def _confirm_view(match_id, submitter_id):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="✅ Xác nhận",
            style=discord.ButtonStyle.success,
            custom_id=f"league:confirm:{match_id}:{submitter_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="❌ Không đúng",
            style=discord.ButtonStyle.danger,
            custom_id=f"league:dispute:{match_id}:{submitter_id}",
        )
    )
    return view


def _unregister_view():
    view = discord.ui.View(timeout=300)
    view.add_item(
        discord.ui.Button(
            label="❌ Hủy đăng ký",
            style=discord.ButtonStyle.danger,
            custom_id="league:unregister",
        )
    )
    return view


def _reset_view(step):
    """Bước 1 và bước 2 của lệnh /reset (xác nhận 2 lần)."""
    view = discord.ui.View(timeout=120)
    if step == 1:
        view.add_item(
            discord.ui.Button(
                label="Tôi chắc chắn (1/2)",
                style=discord.ButtonStyle.danger,
                custom_id="league:reset:1",
            )
        )
    else:
        view.add_item(
            discord.ui.Button(
                label="XÓA TẤT CẢ (2/2)",
                style=discord.ButtonStyle.danger,
                custom_id="league:reset:2",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Thôi, giữ nguyên",
                style=discord.ButtonStyle.secondary,
                custom_id="league:reset:cancel",
            )
        )
    return view


class ScoreModal(discord.ui.Modal):
    def __init__(self, best_of, match_id):
        super().__init__(title="Nộp kết quả trận", timeout=300)
        self.best_of = best_of
        self.match_id = match_id
        label = "BO3" if best_of == 3 else "BO1"
        self.score = discord.ui.TextInput(
            label=f"Tỷ số {label} (theo góc nhìn của bạn)",
            placeholder="2-1" if best_of == 3 else "1-0",
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
        match = ml.get_match(data, self.match_id)
        if not isinstance(match, dict) or match.get("status") != "open":
            return await interaction.followup.send(
                "Trận này đã kết thúc hoặc đổi trạng thái.", ephemeral=True
            )
        if interaction.user.id not in match["players"]:
            return await interaction.followup.send(
                "Bạn không có trong trận này.", ephemeral=True
            )

        best_of = int(match.get("best_of") or 1)
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
                d, self.match_id, interaction.user.id, str(self.score), proof, ml.now()
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
            view=_confirm_view(payload["match_id"], payload["submitter_id"]),
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
            text = "Đăng ký tuần này đã chốt."
        elif status == "left":
            text = f"Đã hủy đăng ký. Còn {payload['count']} người."
        else:
            text = f"Đã đăng ký. Hiện có {payload['count']} người."
            await interaction.followup.send(
                text, ephemeral=True, view=_unregister_view()
            )
            await refresh_registration(interaction.client)
            return
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
    # Vòng lặp giải
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=2)
    async def weekly_loop(self):
        try:
            actions = await update_data(lambda d: ml.ensure_week(d, ml.now()))
            if actions:
                await self._handle_actions(actions)
            # Bot khởi động lại giữa giải: tạo lại chat riêng nếu bị mất.
            await self._ensure_match_threads()
        except Exception:
            log.exception("Lỗi trong vòng lặp giải đấu")

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
                log.exception(
                    "Không gửi được thông báo giải đấu (%s) — thiếu quyền: %s",
                    kind,
                    ", ".join(missing_permissions(channel)) or "không rõ",
                )

    async def _dispatch(self, channel, kind, payload):
        current = ml.now()
        data = await read_data(fresh=True)

        if kind == "open":
            message = await channel.send(
                embed=_registration_embed(data, current), view=RegisterView(), silent=True
            )
            await update_data(lambda d: d["signup"].__setitem__("message_id", message.id))
            return

        if kind == "repost":
            # Đăng lại panel mỗi 8 giờ; bản cũ mất nút để không ai bấm nhầm.
            await self._retire_message(channel, data["signup"].get("message_id"))
            message = await channel.send(
                embed=_registration_embed(data, current), view=RegisterView(), silent=True
            )
            await update_data(lambda d: d["signup"].__setitem__("message_id", message.id))
            return

        if kind == "close_signup":
            await self._close_registration(channel, payload.get("message_id"))
            return

        if kind == "promote":
            await channel.send(
                content=(
                    f"👑 <@{payload['user_id']}> chính thức nhậm chức **Gym Leader** "
                    f"kể từ hôm nay. Chúc mừng!"
                ),
                embed=discord.Embed(description=ml.leader_term_text(data), color=0xF1C40F),
            )
            return

        if kind in ("draw_qualifier", "draw_second", "draw_final"):
            threads = await self._ensure_match_threads()
            fresh = await read_data(fresh=True)
            embed = _draw_embed(kind, payload)
            if kind == "draw_final":
                players = list(payload["players"])
            else:
                players = []
                for match_id in payload.get("match_ids") or []:
                    match = ml.get_match(fresh, match_id)
                    if match:
                        players.extend(match["players"])
            mentions = " ".join(f"<@{uid}>" for uid in players)
            if threads:
                embed.add_field(
                    name="💬 Chat riêng của trận",
                    value="\n".join(f"<#{thread.id}>" for thread in threads.values()),
                    inline=False,
                )
            await channel.send(content=mentions or None, embed=embed, silent=True)
            return

        if kind == "eliminated":
            pairs = ", ".join(
                ml.match_line({"players": item["players"]}) for item in payload.get("matches") or []
            )
            await channel.send(
                content=(
                    f"⛔ Hết hạn báo kết quả — **loại cả 2**: {pairs}"
                    if pairs else "⛔ Hết hạn báo kết quả."
                ),
                silent=True,
            )
            for item in payload.get("matches") or []:
                if item.get("thread_id"):
                    await self._delete_thread(item["thread_id"])
            return

        if kind in ("cancelled", "timeout", "final_timeout"):
            if kind == "timeout":
                text = "⏰ Giải tuần trước chưa xong khi bước sang tuần mới. Gym Leader giữ chức, gia hạn **1 tuần**."
            elif kind == "final_timeout":
                text = "⏰ Chung kết không có kết quả trước hạn. Gym Leader giữ chức, gia hạn **1 tuần**."
            else:
                text = "⚠️ Không đủ người để tiếp tục giải. Gym Leader giữ chức, gia hạn **1 tuần**."
            await channel.send(text, silent=True)
            for thread_id in payload.get("threads") or []:
                await self._delete_thread(thread_id)
            return

        if kind == "walkover":
            await channel.send(
                content=(
                    f"👑 Chỉ có <@{payload['user_id']}> đăng ký **WL{payload['wl']}** "
                    "nên nhậm chức Gym Leader vào **00:00 Thứ 2**."
                )
            )
            return

        if kind == "no_signup":
            await channel.send(
                f"📭 Không ai đăng ký **WL{payload['wl']}**. Gym Leader giữ chức, gia hạn **1 tuần**.",
                silent=True,
            )
            return

        if kind == "ping":
            mentions = _match_mentions(data)
            prefix = f"{mentions} — " if mentions else ""
            await channel.send(
                content=f"🔔 {prefix}nhắc lịch thi đấu **WL{payload['wl']}**.",
                embed=discord.Embed(description=ml.tournament_summary(data, current), color=0xE74C3C),
            )

    async def _close_registration(self, channel, message_id=None):
        if message_id is None:
            data = await read_data(fresh=True)
            message_id = data["signup"].get("message_id")
        if not message_id:
            return
        try:
            await channel.get_partial_message(int(message_id)).edit(
                content="🔒 Đã chốt đăng ký — danh sách đã đưa vào giải.",
                embed=discord.Embed(
                    description="Đăng ký đã chốt. Kết quả ghép cặp ở tin nhắn bên dưới.",
                    color=0x95A5A6,
                ),
                view=None,
            )
        except (discord.NotFound, discord.HTTPException):
            pass

    async def _retire_message(self, channel, message_id):
        """Gỡ nút của panel cũ khi đã đăng bản mới (không xóa, giữ lịch sử)."""
        if not message_id:
            return
        try:
            await channel.get_partial_message(int(message_id)).edit(
                content="⏫ Tin đăng ký mới ở bên dưới.",
                embed=discord.Embed(
                    description="Tin này đã được thay bằng tin đăng ký mới hơn.",
                    color=0x95A5A6,
                ),
                view=None,
            )
        except (discord.NotFound, discord.HTTPException):
            pass

    # ------------------------------------------------------------------ #
    # Chat riêng của từng trận
    # ------------------------------------------------------------------ #

    async def _ensure_match_threads(self):
        """Tạo chat riêng cho mọi trận đang mở chưa có thread. Trả về {match_id: thread}."""
        data = await read_data(fresh=True)
        pending = [m for m in ml.open_matches(data) if not m.get("thread_id")]
        created = {}
        if not pending:
            return created

        parent = await get_channel(self.bot, MATCH_CHANNEL_ID)
        if not isinstance(parent, discord.TextChannel):
            log.warning("Kênh trận đấu %s không phải text channel", MATCH_CHANNEL_ID)
            return created

        guild = parent.guild
        owner = guild.get_member(OWNER_ID)
        if owner is None:
            try:
                owner = await guild.fetch_member(OWNER_ID)
            except discord.HTTPException:
                owner = None

        for match in pending:
            moment = time.monotonic()
            if moment - _THREAD_RETRY.get(match["id"], 0.0) < 600:
                continue
            if len(_THREAD_RETRY) > 200:
                _THREAD_RETRY.clear()
            _THREAD_RETRY[match["id"]] = moment

            members = []
            for user_id in match["players"]:
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        member = None
                if member is not None:
                    members.append(member)

            best_of = "BO3" if match["best_of"] == 3 else "BO1"
            names = " vs ".join(member.display_name[:14] for member in members) or "Trận đấu"
            wl = data["tournament"].get("wl") or 0
            try:
                thread = await parent.create_thread(
                    name=f"⚔️ WL{wl} · {best_of} · {names}"[:95],
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    auto_archive_duration=1440,
                )
            except discord.HTTPException:
                log.exception("Không tạo được chat riêng cho trận #%s", match["id"])
                continue

            invited = list(members)
            if owner is not None and owner not in invited:
                invited.append(owner)
            for member in invited:
                try:
                    await thread.add_user(member)
                except discord.HTTPException:
                    log.warning("Không thêm được %s vào chat riêng", member.id)

            await thread.send(embed=_thread_panel(data, match), view=_submit_view(match["id"]))
            await update_data(
                lambda d, mid=match["id"], tid=thread.id: ml.get_match(d, mid).__setitem__("thread_id", tid)
            )
            created[match["id"]] = thread
        return created

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
            if custom_id.startswith("league:submit:"):
                return await self._on_submit(interaction, int(custom_id.split(":")[2]))
            if custom_id == "league:unregister":
                return await self._on_unregister(interaction)
            if custom_id == "league:reset:1":
                return await self._on_reset(interaction, 1)
            if custom_id == "league:reset:2":
                return await self._on_reset(interaction, 2)
            if custom_id == "league:reset:cancel":
                return await interaction.response.edit_message(
                    content="Đã hủy. Không có dữ liệu nào bị xóa.", embed=None, view=None
                )
            if custom_id.startswith("league:confirm:"):
                _, _, match_id, submitter = custom_id.split(":")
                return await self._on_confirm(interaction, int(match_id), int(submitter))
            if custom_id.startswith("league:dispute:"):
                _, _, match_id, submitter = custom_id.split(":")
                return await self._on_dispute(interaction, int(match_id), int(submitter))
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

    async def _on_unregister(self, interaction):
        await interaction.response.defer(ephemeral=True)
        status, payload = await update_data(
            lambda d: ml.leave_registration(d, interaction.user.id, ml.now())
        )
        if status == "closed":
            text = "Đăng ký tuần này đã chốt."
        elif status == "left":
            text = f"Đã hủy đăng ký. Còn {payload['count']} người."
        elif status == "absent":
            text = "Bạn chưa đăng ký (hoặc đã hủy trước đó)."
        else:
            text = "Không hủy được đăng ký."
        await interaction.followup.send(text, ephemeral=True)
        await refresh_registration(interaction.client)

    async def _on_reset(self, interaction, step):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        if step == 1:
            return await interaction.response.edit_message(
                content=(
                    "⚠️ **XÓA TOÀN BỘ DỮ LIỆU BOT**\n"
                    "Sẽ xóa: Gym Leader, lịch sử trận, bảng xếp hạng, đăng ký, số WL, "
                    "chat riêng của các trận.\n"
                    "**Không thể hoàn tác.** Bấm lần nữa để xác nhận."
                ),
                embed=None,
                view=_reset_view(2),
            )

        await interaction.response.edit_message(
            content="🧹 Đang xóa toàn bộ dữ liệu...", embed=None, view=None
        )
        await self._wipe_everything()
        await interaction.followup.send(
            "✅ Đã reset toàn bộ dữ liệu. Panel **WL1** sẽ tự mở trong ít phút.",
            ephemeral=True,
        )

    async def _wipe_everything(self):
        data = await read_data(fresh=True)
        thread_ids = ml.all_thread_ids(data)
        message_id = data["signup"].get("message_id")

        def apply(d):
            d.clear()
            d.update(ml.reset_state())
            return True

        await update_data(apply)

        for thread_id in thread_ids:
            await self._delete_thread(thread_id)
        channel = await get_channel(self.bot, GYM_CHANNEL_ID)
        if channel is not None:
            await self._retire_message(channel, message_id)

    async def _on_submit(self, interaction, match_id):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message(
                "Nút này chỉ dùng trong chat riêng của trận.", ephemeral=True
            )
        data = await read_data(fresh=True)
        match = ml.get_match(data, match_id)
        if not isinstance(match, dict) or match.get("status") != "open":
            return await interaction.response.send_message(
                "Trận này đã kết thúc.", ephemeral=True
            )
        if interaction.user.id not in match["players"]:
            return await interaction.response.send_message(
                "Bạn không có trong trận này.", ephemeral=True
            )
        await interaction.response.send_modal(
            ScoreModal(int(match.get("best_of") or 1), match_id)
        )

    async def _on_confirm(self, interaction, match_id, submitter_id):
        await interaction.response.defer(thinking=True)
        data = await read_data(fresh=True)
        match = ml.get_match(data, match_id)
        if not isinstance(match, dict) or match.get("status") != "open":
            return await interaction.followup.send(
                "Trận này đã kết thúc hoặc đổi trạng thái.", ephemeral=True
            )
        submission = ml.get_submission(data, match_id, submitter_id)
        if not isinstance(submission, dict):
            return await interaction.followup.send(
                "Không tìm thấy biên bản này.", ephemeral=True
            )
        opponent = next((uid for uid in match["players"] if uid != submitter_id), None)
        if interaction.user.id not in (opponent, OWNER_ID):
            return await interaction.followup.send(
                "Chỉ đối thủ của người nộp (hoặc owner) mới xác nhận được.", ephemeral=True
            )
        if submission.get("status") == "confirmed":
            return await interaction.followup.send(
                "Biên bản này đã được xác nhận rồi.", ephemeral=True
            )

        status, payload = await update_data(
            lambda d: ml.confirm_result(d, match_id, submitter_id, ml.now())
        )
        if status == "already":
            return await interaction.followup.send(
                "Biên bản này đã được xác nhận rồi.", ephemeral=True
            )
        if status != "ok":
            return await interaction.followup.send(
                "Không xác nhận được, trạng thái giải đã thay đổi.", ephemeral=True
            )

        await self._announce_result(payload)
        try:
            await interaction.followup.send("✅ Đã xác nhận kết quả.")
        except discord.HTTPException:
            pass

        await self._delete_thread(interaction.channel.id)
        await self._progress()

    async def _on_dispute(self, interaction, match_id, submitter_id):
        await interaction.response.defer(thinking=True)
        data = await read_data(fresh=True)
        match = ml.get_match(data, match_id)
        if not isinstance(match, dict) or match.get("status") != "open":
            return await interaction.followup.send(
                "Trận này đã kết thúc hoặc đổi trạng thái.", ephemeral=True
            )
        opponent = next((uid for uid in match["players"] if uid != submitter_id), None)
        if interaction.user.id not in (opponent, OWNER_ID):
            return await interaction.followup.send(
                "Chỉ đối thủ của người nộp (hoặc owner) mới báo sai được.", ephemeral=True
            )
        status, payload = await update_data(
            lambda d: ml.dispute_result(d, match_id, submitter_id, interaction.user.id, ml.now())
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

    async def _progress(self):
        """Chạy tiếp vòng đấu sau khi có kết quả (ghép vòng mới, chốt danh sách...)."""
        actions = await update_data(lambda d: ml.ensure_week(d, ml.now()))
        if actions:
            await self._handle_actions(actions)

    async def _announce_result(self, payload):
        channel = await get_channel(self.bot, GYM_CHANNEL_ID)
        if channel is None:
            return
        phase = payload["phase"]
        winner = payload["winner_id"]
        if phase == "qualifier":
            title = "🏆 KẾT QUẢ VÒNG LOẠI BO1"
            text = f"<@{winner}> thắng và đi tiếp."
        elif phase == "second":
            title = "🏆 KẾT QUẢ GHÉP CẶP LẦN 2"
            text = f"<@{winner}> thắng. Danh sách random sẽ chốt khi hết vòng."
        else:
            title = "👑 KẾT QUẢ CHUNG KẾT BO3"
            text = (
                f"<@{winner}> thắng chung kết và sẽ nhậm chức **Gym Leader** "
                "vào **00:00 Thứ 2**."
            )
        embed = discord.Embed(
            title=title,
            color=0x2ECC71,
            description=f"{text}\n📊 Tỷ số: `{payload.get('score')}`",
        )
        embed.set_footer(text="Biên bản do người chơi nộp, đã được đối thủ xác nhận.")
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

    @app_commands.command(name="luat", description="Xem luật thi đấu của Gym Dark.")
    @is_gym_channel()
    async def luat(self, interaction: discord.Interaction):
        await interaction.response.send_message(embeds=rules_embeds())

    @app_commands.command(name="gym", description="Xem Gym Leader và tình trạng giải hiện tại.")
    @is_gym_channel()
    async def gym(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await read_data()
        embed = discord.Embed(title="🏟️ GYM DARK TGA", color=0x2ECC71)
        embed.add_field(name="👑 Leader hiện tại", value=ml.leader_term_text(data), inline=False)
        embed.add_field(name="📅 Giải hiện tại", value=ml.week_summary(data, ml.now()), inline=False)
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
            text = "Đăng ký tuần này đã chốt."
        elif status == "left":
            text = f"Đã hủy đăng ký. Còn {payload['count']} người."
        else:
            text = f"Đã đăng ký. Hiện có {payload['count']} người."
            await interaction.followup.send(
                text, ephemeral=True, view=_unregister_view()
            )
            await refresh_registration(interaction.client)
            return
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
        labels = {
            "qualifier": "BO1 vòng loại",
            "second": "BO1 ghép cặp lần 2",
            "final": "BO3 chung kết",
            "qualifier_void": "Vòng loại — bỏ (không báo kết quả)",
            "second_void": "Ghép cặp lần 2 — bỏ (không báo kết quả)",
            "final_void": "Chung kết — bỏ (không báo kết quả)",
            "legacy": "Hệ thống cũ",
        }
        for item in history:
            label = labels.get(item.get("type"), "Trận")
            if item.get("winner_id"):
                value = (
                    f"🏆 <@{item.get('winner_id')}> thắng <@{item.get('loser_id')}>\n"
                    f"📅 `{item.get('at', '—')}`"
                )
            else:
                players = ", ".join(f"<@{uid}>" for uid in item.get("players") or [])
                value = f"⛔ Không báo kết quả: {players}\n📅 `{item.get('at', '—')}`"
            embed.add_field(
                name=f"#{item.get('match_id')} — {label}",
                value=value,
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

    @app_commands.command(name="ket_qua", description="[OWNER] Chốt người thắng của trận đang mở.")
    @is_gym_channel()
    async def ket_qua(self, interaction: discord.Interaction, nguoi_thang: discord.Member):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer()
        current = ml.now()
        data = await read_data(fresh=True)
        match = ml.match_for_user(data, nguoi_thang.id)
        if match is None:
            return await fail_deferred(
                interaction, f"{nguoi_thang.mention} không có trận nào đang mở."
            )
        thread_id = match.get("thread_id")

        status, payload = await update_data(
            lambda d: ml.force_result(d, nguoi_thang.id, current, match["id"])
        )
        if status != "ok":
            return await fail_deferred(interaction, "Không chốt được trận này, thử lại sau.")

        embed = discord.Embed(
            title=f"✅ ĐÃ CHỐT — MATCH #{payload['match_id']}",
            color=0x2ECC71,
            description=(
                f"🏆 <@{payload['winner_id']}> thắng <@{payload['loser_id']}> "
                f"({ml.phase_label(payload['phase'])})."
            ),
        )
        await interaction.followup.send(embed=embed)
        if thread_id:
            await self._delete_thread(thread_id)
        await self._progress()

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
            data["leader_elect"] = None
            ml.get_player(data, member.id)["titles"] += 1
            return True

        await update_data(apply)
        await interaction.followup.send(
            f"👑 Đã gán {member.mention} làm Gym Leader cho tuần sau."
        )

    @app_commands.command(name="huy_giai", description="[OWNER] Hủy giải đang chạy và gia hạn Leader.")
    @is_gym_channel()
    async def huy_giai(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer()
        current = ml.now()
        data = await read_data(fresh=True)
        thread_ids = ml.all_thread_ids(data)

        def apply(d):
            ml.extend_term(d, current)
            d["leader_elect"] = None
            for match in (d["tournament"].get("matches") or {}).values():
                if match.get("status") == "open":
                    match["status"] = "void"
                    match["ended_at"] = current.isoformat()
            d["tournament"]["phase"] = "done"
            d["tournament"]["flags"]["cancelled"] = True
            return True

        await update_data(apply)
        for thread_id in thread_ids:
            await self._delete_thread(thread_id)
        await interaction.followup.send("Đã hủy giải. Leader được gia hạn 1 tuần.")

    @app_commands.command(
        name="tao_dang_ky", description="[OWNER] Đăng lại tin đăng ký thách đấu ngay bây giờ."
    )
    @is_gym_channel()
    async def tao_dang_ky(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        channel = None
        try:
            # Mở lại đăng ký của tuần hiện tại để panel luôn dùng được (kể cả sau /reset).
            data = await update_data(lambda d: ml.force_open_signup(d, ml.now()))
            channel = await get_channel(self.bot, GYM_CHANNEL_ID)
            if channel is None:
                return await interaction.followup.send(
                    f"Không tìm thấy kênh Gym (`{GYM_CHANNEL_ID}`).", ephemeral=True
                )
            await self._retire_message(channel, data["signup"].get("message_id"))
            message = await channel.send(
                embed=_registration_embed(data, ml.now()), view=RegisterView(), silent=True
            )
            await update_data(lambda d: d["signup"].__setitem__("message_id", message.id))
        except discord.Forbidden:
            log.exception("Thiếu quyền khi đăng panel đăng ký")
            missing = missing_permissions(channel) if channel is not None else []
            detail = ", ".join(missing) or "Send Messages, Embed Links, Attach Files"
            return await interaction.followup.send(
                f"Bot thiếu quyền trong kênh <#{GYM_CHANNEL_ID}>: **{detail}**.\n"
                "Cấp ở **Edit Channel → Permissions** cho role của bot (hoặc mời lại bot "
                "bằng link có đủ quyền).",
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001 - owner cần thấy lỗi thật để báo lại
            log.exception("Lỗi /tao_dang_ky")
            return await interaction.followup.send(
                f"Lỗi khi đăng panel: `{type(exc).__name__}: {str(exc)[:300]}`",
                ephemeral=True,
            )
        await interaction.followup.send(
            f"Đã đăng tin đăng ký **WL{data['signup'].get('wl') or 1}**.", ephemeral=True
        )

    @app_commands.command(
        name="bat_dau", description="[OWNER] Chốt đăng ký và mở giải ngay (để test)."
    )
    @is_gym_channel()
    async def bat_dau(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        status, actions = await update_data(
            lambda d: ml.start_tournament_now(d, ml.now())
        )
        if status == "busy":
            return await interaction.followup.send(
                "Đang có giải chạy rồi.", ephemeral=True
            )
        if status == "few":
            return await interaction.followup.send(
                "Cần ít nhất 2 người đăng ký mới mở giải.", ephemeral=True
            )
        try:
            await self._handle_actions(actions)
            await self._ensure_match_threads()
        except Exception as exc:  # noqa: BLE001 - owner cần thấy lỗi thật để báo lại
            log.exception("Lỗi /bat_dau")
            return await interaction.followup.send(
                f"Đã mở giải nhưng gửi thông báo lỗi: `{type(exc).__name__}: {str(exc)[:300]}`",
                ephemeral=True,
            )
        await interaction.followup.send("Đã chốt đăng ký và mở giải.", ephemeral=True)

    @app_commands.command(
        name="reset", description="[OWNER] Xóa toàn bộ dữ liệu bot (xác nhận 2 lần)."
    )
    @is_gym_channel()
    async def reset(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        embed = discord.Embed(
            title="⚠️ RESET TOÀN BỘ DỮ LIỆU",
            description=(
                "Lệnh này xóa **tất cả**: Gym Leader, lịch sử trận, bảng xếp hạng, "
                "đăng ký, số WL và chat riêng của các trận.\n\n"
                "**Không thể hoàn tác.** Bấm nút bên dưới để bắt đầu xác nhận."
            ),
            color=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed, view=_reset_view(1), ephemeral=True)


async def setup(bot):
    await bot.add_cog(League(bot))
