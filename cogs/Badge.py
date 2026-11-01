"""Tính năng đánh lấy huy hiệu (badge) của Dark Gym.

Luồng hoạt động:
- Owner chạy ``/dat_council`` mention 5 người giữ huy hiệu theo thứ tự từ thấp
  đến cao (4 Shadow Council + Gym Leader).
- Member bấm nút trên panel (``/panel_huy_hieu``) hoặc dùng ``/battle_bage``.
- Bot mở **1 phòng** (private thread) gồm member + 5 người giữ huy hiệu; tối đa
  **2 người xếp hàng chờ**. Khi phòng đóng, người chờ đầu tiên được mở phòng.
- Shadow Council / Admin bấm ``Thắng`` (trao huy hiệu) hoặc ``Thua`` (đóng
  phòng). Bot ghi log thời gian nhận huy hiệu vào PostgreSQL, tag owner để đóng
  phòng và nhường slot.
- ``/check_history_bage`` xem lịch sử nhận huy hiệu (ngày giờ).
"""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.core import (
    GYM_CHANNEL_ID,
    MATCH_CHANNEL_ID,
    OWNER_ID,
    get_pool,
    is_gym_channel,
    is_owner,
)

log = logging.getLogger(__name__)

MAX_QUEUE = 2
COLOR = 0x8E44AD
JOIN_CUSTOM_ID = "badge:join"


# --------------------------------------------------------------------------- #
# Lưu trữ
# --------------------------------------------------------------------------- #

_tables_ready = False


def _init_tables():
    global _tables_ready
    if _tables_ready:
        return
    with get_pool().connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS badge_config (
                guild_id BIGINT PRIMARY KEY,
                holder_ids BIGINT[] NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS badge_rooms (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                position INT NOT NULL,
                thread_id BIGINT UNIQUE,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS badge_queue (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS badge_history (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                position INT NOT NULL,
                awarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS badge_history_user "
            "ON badge_history (guild_id, user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS badge_rooms_open "
            "ON badge_rooms (guild_id, status)"
        )
    _tables_ready = True


def _ensure_tables(attempts=5, delay=3.0):
    """Tạo bảng có thử lại. Lỗi mạng tạm thời không được làm bot chết."""
    global _tables_ready
    for attempt in range(1, attempts + 1):
        try:
            _init_tables()
            return True
        except Exception:  # noqa: BLE001 - lỗi DB tạm thời
            if attempt == attempts:
                log.exception("Không tạo được bảng huy hiệu sau %s lần thử", attempts)
                return False
            log.warning(
                "Tạo bảng huy hiệu thất bại (lần %s/%s), thử lại sau %.0fs",
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
    return _tables_ready


def _get_holders_sync(guild_id):
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT holder_ids FROM badge_config WHERE guild_id = %s", (guild_id,)
        ).fetchone()
    return [int(x) for x in row[0]] if row else []


def _set_holders_sync(guild_id, holder_ids):
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO badge_config (guild_id, holder_ids, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (guild_id) DO UPDATE
                SET holder_ids = EXCLUDED.holder_ids, updated_at = NOW()
            """,
            (guild_id, holder_ids),
        )


def _history_sync(guild_id, user_id):
    with get_pool().connection() as conn:
        return conn.execute(
            """
            SELECT position, awarded_at
            FROM badge_history
            WHERE guild_id = %s AND user_id = %s
            ORDER BY position
            """,
            (guild_id, user_id),
        ).fetchall()


def _queue_index(conn, guild_id, user_id):
    rows = conn.execute(
        "SELECT user_id FROM badge_queue WHERE guild_id = %s ORDER BY created_at",
        (guild_id,),
    ).fetchall()
    for index, row in enumerate(rows, start=1):
        if int(row[0]) == int(user_id):
            return index
    return 0


def _join_sync(guild_id, user_id):
    """Đăng ký thách đấu. Trả về (trạng thái, dữ liệu)."""
    with get_pool().connection() as conn:
        # Khóa theo guild để hai người bấm cùng lúc không tạo 2 phòng.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (guild_id,))
        row = conn.execute(
            "SELECT holder_ids FROM badge_config WHERE guild_id = %s", (guild_id,)
        ).fetchone()
        if not row:
            return "no_config", {}
        holders = [int(x) for x in row[0]]
        if int(user_id) in holders:
            return "holder", {}

        earned = conn.execute(
            "SELECT COUNT(*) FROM badge_history WHERE guild_id = %s AND user_id = %s",
            (guild_id, user_id),
        ).fetchone()[0]
        position = int(earned) + 1
        if position > len(holders):
            return "done", {"total": len(holders)}

        if conn.execute(
            "SELECT 1 FROM badge_rooms "
            "WHERE guild_id = %s AND user_id = %s AND status = 'open'",
            (guild_id, user_id),
        ).fetchone():
            return "in_room", {}

        if conn.execute(
            "SELECT 1 FROM badge_queue WHERE guild_id = %s AND user_id = %s",
            (guild_id, user_id),
        ).fetchone():
            return "in_queue", {"position": _queue_index(conn, guild_id, user_id)}

        busy = conn.execute(
            "SELECT 1 FROM badge_rooms WHERE guild_id = %s AND status = 'open' LIMIT 1",
            (guild_id,),
        ).fetchone()
        if busy:
            waiting = conn.execute(
                "SELECT COUNT(*) FROM badge_queue WHERE guild_id = %s", (guild_id,)
            ).fetchone()[0]
            if waiting >= MAX_QUEUE:
                return "queue_full", {"max": MAX_QUEUE}
            conn.execute(
                "INSERT INTO badge_queue (guild_id, user_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (guild_id, user_id),
            )
            return "queued", {"position": int(waiting) + 1, "badge": position}

        room_id = conn.execute(
            "INSERT INTO badge_rooms (guild_id, user_id, position) "
            "VALUES (%s, %s, %s) RETURNING id",
            (guild_id, user_id, position),
        ).fetchone()[0]
        return "room", {"room_id": int(room_id), "position": position}


def _close_room_sync(room_id, won):
    """Đóng phòng, ghi log nếu thắng, và mở phòng cho người kế tiếp trong hàng đợi."""
    with get_pool().connection() as conn:
        first = conn.execute(
            "SELECT guild_id FROM badge_rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if not first:
            return "gone", {}
        guild_id = int(first[0])
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (guild_id,))

        row = conn.execute(
            "SELECT guild_id, user_id, position, thread_id, status "
            "FROM badge_rooms WHERE id = %s FOR UPDATE",
            (room_id,),
        ).fetchone()
        if not row or row[4] != "open":
            return "gone", {}

        guild_id, user_id, position, thread_id = (
            int(row[0]),
            int(row[1]),
            int(row[2]),
            row[3],
        )

        if won:
            conn.execute(
                "INSERT INTO badge_history (guild_id, user_id, position) "
                "VALUES (%s, %s, %s)",
                (guild_id, user_id, position),
            )
        conn.execute("UPDATE badge_rooms SET status = 'closed' WHERE id = %s", (room_id,))

        nxt = conn.execute(
            "SELECT user_id FROM badge_queue WHERE guild_id = %s "
            "ORDER BY created_at LIMIT 1",
            (guild_id,),
        ).fetchone()
        next_user = None
        next_room = None
        next_position = None
        if nxt:
            next_user = int(nxt[0])
            conn.execute(
                "DELETE FROM badge_queue WHERE guild_id = %s AND user_id = %s",
                (guild_id, next_user),
            )
            earned = conn.execute(
                "SELECT COUNT(*) FROM badge_history "
                "WHERE guild_id = %s AND user_id = %s",
                (guild_id, next_user),
            ).fetchone()[0]
            next_position = int(earned) + 1
            next_room = int(
                conn.execute(
                    "INSERT INTO badge_rooms (guild_id, user_id, position) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (guild_id, next_user, next_position),
                ).fetchone()[0]
            )

    return "ok", {
        "guild_id": guild_id,
        "user_id": user_id,
        "position": position,
        "thread_id": int(thread_id) if thread_id else None,
        "won": bool(won),
        "next_user": next_user,
        "next_room": next_room,
        "next_position": next_position,
    }


def _set_thread_sync(room_id, thread_id):
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE badge_rooms SET thread_id = %s WHERE id = %s",
            (thread_id, room_id),
        )


def _open_rooms_sync(guild_id):
    with get_pool().connection() as conn:
        return [
            (
                int(r[0]),
                int(r[1]) if r[1] else None,
                int(r[2].timestamp()) if r[2] else 0,
            )
            for r in conn.execute(
                "SELECT id, thread_id, created_at FROM badge_rooms "
                "WHERE guild_id = %s AND status = 'open'",
                (guild_id,),
            ).fetchall()
        ]


# --------------------------------------------------------------------------- #
# Tiện ích Discord
# --------------------------------------------------------------------------- #

async def get_channel(client, channel_id):
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


def can_judge(user, holders) -> bool:
    """Shadow Council, Admin hoặc owner được bấm kết quả."""
    if is_owner(user.id) or user.id in holders:
        return True
    return bool(getattr(user, "guild_permissions", None) and user.guild_permissions.administrator)


def _room_embed(position, total, challenger, holder):
    embed = discord.Embed(
        title=f"🏅 TRẬN HUY HIỆU {position}",
        color=COLOR,
        description=(
            f"{challenger.mention} thách đấu {holder.mention} để nhận "
            f"**Huy hiệu {position}/{total}**.\n\n"
            "Kết quả do **Shadow Council** hoặc **Admin** bấm bên dưới.\n"
            "`Thắng` = trao huy hiệu và ghi log · `Thua` = đóng phòng, không trao."
        ),
    )
    embed.set_footer(text=f"Huy hiệu {position}/{total} · Dark Gym")
    return embed


def _room_view(room_id):
    """Nút kết quả. Callback rỗng: xử lý thật nằm ở `on_interaction` để nút
    vẫn hoạt động sau khi bot restart (view không còn trong bộ nhớ).
    """
    view = discord.ui.View(timeout=None)
    for action, label, emoji, style in (
        ("win", "Thắng – trao huy hiệu", "✅", discord.ButtonStyle.success),
        ("lose", "Thua – đóng phòng", "❌", discord.ButtonStyle.danger),
    ):
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"badge:{action}:{room_id}",
        )
        # `Button.__init__` không nhận callback; gán trực tiếp để tránh lỗi
        # "unknown callback" khi view còn trong bộ nhớ.
        button.callback = _noop
        view.add_item(button)
    return view


async def _noop(interaction: discord.Interaction):
    # Interaction được ack trong on_interaction; ở đây không làm gì.
    return


async def _fetch_member(guild, user_id):
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    return member


async def open_room(client, guild, room_id, user_id, position, holders):
    """Tạo private thread cho một phòng đã ghi trong DB. Trả về thread."""
    parent = await get_channel(client, MATCH_CHANNEL_ID)
    if not isinstance(parent, discord.TextChannel):
        raise RuntimeError(f"Kênh trận {MATCH_CHANNEL_ID} không phải text channel.")
    if position > len(holders):
        raise RuntimeError("Cấu hình huy hiệu đã thay đổi, hãy chạy /dat_council lại.")

    challenger = await _fetch_member(guild, user_id)
    if challenger is None:
        raise RuntimeError(f"Không tìm thấy member {user_id} trong server.")

    holder_id = holders[position - 1]
    holder = await _fetch_member(guild, holder_id)

    thread = await parent.create_thread(
        name=f"🏅 Huy hiệu {position} · {challenger.display_name}"[:95],
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=1440,
    )

    invited = [challenger]
    for hid in holders:
        member = await _fetch_member(guild, hid)
        if member is not None and member not in invited:
            invited.append(member)
    owner = await _fetch_member(guild, OWNER_ID)
    if owner is not None and owner not in invited:
        invited.append(owner)

    for member in invited:
        try:
            await thread.add_user(member)
        except discord.HTTPException:
            log.warning("Không thêm được %s vào phòng huy hiệu", member.id)

    await _set_thread_sync(room_id, thread.id)

    mentions = " ".join(m.mention for m in invited)
    embed = _room_embed(
        position,
        len(holders),
        challenger,
        holder or challenger,
    )
    await thread.send(content=mentions, embed=embed, view=_room_view(room_id))
    return thread


# --------------------------------------------------------------------------- #
# Nút đăng ký trên panel
# --------------------------------------------------------------------------- #

class BadgeJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Đăng ký thách đấu",
        emoji="🏅",
        style=discord.ButtonStyle.success,
        custom_id=JOIN_CUSTOM_ID,
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_join(interaction)


async def handle_join(interaction: discord.Interaction):
    """Dùng chung cho nút trên panel và lệnh /battle_bage."""
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "Chỉ dùng được trong server.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    if not await asyncio.to_thread(_ensure_tables):
        return await interaction.followup.send(
            "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
            ephemeral=True,
        )
    try:
        status, payload = await asyncio.to_thread(_join_sync, guild.id, interaction.user.id)
    except Exception as exc:  # noqa: BLE001 - người chơi cần thông báo gọn
        log.exception("Lỗi đăng ký huy hiệu")
        return await interaction.followup.send(
            "Có lỗi khi đăng ký, thử lại sau.", ephemeral=True
        )

    if status == "no_config":
        return await interaction.followup.send(
            "Owner chưa cấu hình người giữ huy hiệu. Hãy dùng `/dat_council`.",
            ephemeral=True,
        )
    if status == "holder":
        return await interaction.followup.send(
            "Bạn là người giữ huy hiệu nên không thể thách đấu.", ephemeral=True
        )
    if status == "done":
        return await interaction.followup.send(
            f"Bạn đã có đủ **{payload['total']} huy hiệu**. Không còn gì để chinh phục!",
            ephemeral=True,
        )
    if status == "in_room":
        return await interaction.followup.send(
            "Bạn đang có phòng đấu rồi. Vào phòng đó để tiếp tục.", ephemeral=True
        )
    if status == "in_queue":
        return await interaction.followup.send(
            f"Bạn đang trong hàng đợi ở vị trí **{payload['position']}/{MAX_QUEUE}**.",
            ephemeral=True,
        )
    if status == "queue_full":
        return await interaction.followup.send(
            f"Đã có **{payload['max']} người** xếp hàng chờ. Chờ trận hiện tại xong rồi đăng ký lại.",
            ephemeral=True,
        )
    if status == "queued":
        return await interaction.followup.send(
            f"Đã vào hàng đợi ở vị trí **{payload['position']}/{MAX_QUEUE}** "
            f"(sẽ đánh **Huy hiệu {payload['badge']}**).\n"
            "Khi trận hiện tại kết thúc, bot sẽ mở phòng cho bạn.",
            ephemeral=True,
        )

    # status == "room"
    holders = await asyncio.to_thread(_get_holders_sync, guild.id)
    try:
        thread = await open_room(
            interaction.client, guild, payload["room_id"], interaction.user.id,
            payload["position"], holders,
        )
    except Exception as exc:  # noqa: BLE001 - dọn phòng để không kẹt slot
        log.exception("Không tạo được phòng huy hiệu")
        try:
            result = await asyncio.to_thread(_close_room_sync, payload["room_id"], False)
            await _promote_next(interaction.client, guild, result[1])
        except Exception:
            log.exception("Không dọn được phòng huy hiệu lỗi")
        return await interaction.followup.send(
            f"Không tạo được phòng: `{type(exc).__name__}: {str(exc)[:200]}`",
            ephemeral=True,
        )

    await interaction.followup.send(
        f"Đã mở phòng cho bạn: {thread.mention} — đánh **Huy hiệu {payload['position']}**.",
        ephemeral=True,
    )


async def _promote_next(client, guild, payload):
    """Mở phòng cho người đầu hàng đợi (nếu có)."""
    if not payload or not payload.get("next_room"):
        return None
    holders = await asyncio.to_thread(_get_holders_sync, guild.id)
    try:
        return await open_room(
            client, guild, payload["next_room"], payload["next_user"],
            payload["next_position"], holders,
        )
    except Exception:  # noqa: BLE001
        log.exception("Không mở được phòng cho người kế tiếp")
        # Đóng phòng vừa tạo để nhường slot cho người sau trong hàng đợi.
        result = await asyncio.to_thread(_close_room_sync, payload["next_room"], False)
        if result[0] == "ok":
            await _promote_next(client, guild, result[1])
        return None


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #

class Badge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Không để lỗi DB tạm thời làm bot không khởi động được.
        if not await asyncio.to_thread(_ensure_tables):
            log.error("Tính năng huy hiệu chưa sẵn sàng, sẽ thử lại trong vòng lặp dọn phòng.")
        self.bot.add_view(BadgeJoinView())
        self.reconcile_loop.start()

    def cog_unload(self):
        self.reconcile_loop.cancel()

    # ------------------------------------------------------------------ #
    # Nút trong phòng (custom_id động)
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id") or ""
        if custom_id.startswith("badge:win:") or custom_id.startswith("badge:lose:"):
            try:
                _, action, raw_id = custom_id.split(":")
                await self._on_result(interaction, int(raw_id), action == "win")
            except Exception:
                log.exception("Lỗi khi xử lý nút huy hiệu %s", custom_id)
                text = "Có lỗi xảy ra, thử lại sau."
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)

    async def _on_result(self, interaction, room_id, won):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Chỉ dùng được trong server.", ephemeral=True
            )
        holders = await asyncio.to_thread(_get_holders_sync, guild.id)
        if not can_judge(interaction.user, holders):
            return await interaction.response.send_message(
                "Chỉ **Shadow Council** hoặc **Admin** được bấm kết quả.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )
        status, payload = await asyncio.to_thread(_close_room_sync, room_id, won)
        if status != "ok":
            return await interaction.followup.send(
                "Phòng này đã được xử lý rồi.", ephemeral=True
            )

        # Đóng phòng: khoá + lưu trữ thread để owner xoá khi rảnh, slot đã được
        # giải phóng ngay trong database nên người kế tiếp không phải chờ.
        thread = await get_channel(self.bot, payload["thread_id"]) if payload.get("thread_id") else None
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(
                    "🔒 Phòng đã có kết quả. Cảm ơn mọi người — phòng sẽ được đóng lại."
                )
            except discord.HTTPException:
                pass
            try:
                await thread.edit(locked=True, archived=True)
            except discord.HTTPException:
                log.warning("Không khoá được phòng huy hiệu %s", payload.get("thread_id"))

        # Thông báo trong kênh Gym + tag owner để đóng phòng, nhường slot.
        channel = await get_channel(self.bot, GYM_CHANNEL_ID)
        if channel is not None:
            if payload["won"]:
                text = (
                    f"🏅 <@{payload['user_id']}> đã nhận **Huy hiệu {payload['position']}**! "
                    f"<@{OWNER_ID}> phòng đã xong, đã nhường slot."
                )
            else:
                text = (
                    f"❌ <@{payload['user_id']}> chưa vượt qua **Huy hiệu "
                    f"{payload['position']}**. <@{OWNER_ID}> phòng đã xong, đã nhường slot."
                )
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                log.warning("Không gửi được thông báo kết quả huy hiệu")

        next_thread = await _promote_next(self.bot, guild, payload)
        if next_thread is not None and channel is not None:
            try:
                await channel.send(
                    f"➡️ Phòng tiếp theo: <@{payload['next_user']}> — "
                    f"**Huy hiệu {payload['next_position']}** {next_thread.mention}"
                )
            except discord.HTTPException:
                pass

        await interaction.followup.send("Đã ghi nhận kết quả.", ephemeral=True)

    # ------------------------------------------------------------------ #
    # Dọn phòng bị mất thread (bot restart / ai đó xoá tay)
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=15)
    async def reconcile_loop(self):
        try:
            if not await asyncio.to_thread(_ensure_tables):
                return
            for guild in self.bot.guilds:
                rooms = await asyncio.to_thread(_open_rooms_sync, guild.id)
                for room_id, thread_id, created_at in rooms:
                    if not thread_id:
                        # Phòng tạo dở (bot tắt giữa chừng) không có thread:
                        # chỉ đóng khi đã quá 5 phút để không tranh slot.
                        if time.time() - created_at < 300:
                            continue
                        log.warning(
                            "Phòng huy hiệu %s thiếu thread, đóng để nhường slot", room_id
                        )
                        result = await asyncio.to_thread(_close_room_sync, room_id, False)
                        if result[0] == "ok":
                            await _promote_next(self.bot, guild, result[1])
                        continue
                    thread = await get_channel(self.bot, thread_id)
                    if isinstance(thread, discord.Thread):
                        continue
                    log.warning("Phòng huy hiệu %s mất thread, đóng để nhường slot", room_id)
                    result = await asyncio.to_thread(_close_room_sync, room_id, False)
                    if result[0] == "ok":
                        await _promote_next(self.bot, guild, result[1])
        except Exception:
            log.exception("Lỗi dọn phòng huy hiệu")

    @reconcile_loop.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Lệnh
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="dat_council",
        description="[OWNER] Đặt 5 người giữ huy hiệu theo thứ tự từ thấp đến cao.",
    )
    @app_commands.describe(
        nguoi_1="Huy hiệu 1 (thấp nhất)",
        nguoi_2="Huy hiệu 2",
        nguoi_3="Huy hiệu 3",
        nguoi_4="Huy hiệu 4",
        nguoi_5="Huy hiệu 5 (cao nhất, thường là Gym Leader)",
    )
    async def dat_council(
        self,
        interaction: discord.Interaction,
        nguoi_1: discord.Member,
        nguoi_2: discord.Member,
        nguoi_3: discord.Member,
        nguoi_4: discord.Member,
        nguoi_5: discord.Member,
    ):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        members = [nguoi_1, nguoi_2, nguoi_3, nguoi_4, nguoi_5]
        if len({m.id for m in members}) != 5:
            return await interaction.response.send_message(
                "5 người phải khác nhau.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )
        await asyncio.to_thread(
            _set_holders_sync, interaction.guild_id, [m.id for m in members]
        )
        lines = "\n".join(
            f"**Huy hiệu {i}** — {m.mention}" for i, m in enumerate(members, start=1)
        )
        await interaction.followup.send(
            f"Đã đặt thứ tự huy hiệu:\n{lines}", ephemeral=True
        )

    @app_commands.command(
        name="panel_huy_hieu",
        description="[OWNER] Đăng panel đăng ký đánh lấy huy hiệu.",
    )
    @is_gym_channel()
    async def panel_huy_hieu(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )
        holders = await asyncio.to_thread(_get_holders_sync, interaction.guild_id)
        if not holders:
            return await interaction.followup.send(
                "Chưa cấu hình. Chạy `/dat_council` trước.", ephemeral=True
            )
        channel = await get_channel(self.bot, GYM_CHANNEL_ID)
        if channel is None:
            return await interaction.followup.send(
                f"Không tìm thấy kênh Gym (`{GYM_CHANNEL_ID}`).", ephemeral=True
            )

        guild = interaction.guild
        lines = []
        for index, holder_id in enumerate(holders, start=1):
            member = await _fetch_member(guild, holder_id) if guild else None
            name = member.mention if member else f"<@{holder_id}>"
            lines.append(f"**Huy hiệu {index}** — {name}")

        embed = discord.Embed(
            title="🏅 THÁCH ĐẤU LẤY HUY HIỆU",
            color=COLOR,
            description=(
                "Đánh bại lần lượt từng người giữ huy hiệu để nhận đủ bộ huy hiệu.\n\n"
                + "\n".join(lines)
                + "\n\n**Cách tham gia:** bấm nút bên dưới hoặc dùng `/battle_bage`.\n"
                f"Mỗi lúc chỉ mở **1 phòng**, tối đa **{MAX_QUEUE} người** xếp hàng chờ."
            ),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Dark Gym · Shadow Council")
        try:
            message = await channel.send(embed=embed, view=BadgeJoinView())
        except discord.Forbidden:
            return await interaction.followup.send(
                f"Bot thiếu quyền trong kênh <#{GYM_CHANNEL_ID}> (Send Messages / Embed Links).",
                ephemeral=True,
            )
        await interaction.followup.send(
            f"Đã đăng panel huy hiệu: {message.jump_url}", ephemeral=True
        )

    @app_commands.command(
        name="battle_bage",
        description="Đăng ký thách đấu lấy huy hiệu.",
    )
    @is_gym_channel()
    async def battle_bage(self, interaction: discord.Interaction):
        await handle_join(interaction)

    @app_commands.command(
        name="check_history_bage",
        description="Xem lịch sử nhận huy hiệu (ngày giờ).",
    )
    @app_commands.describe(member="Để trống để xem của chính bạn")
    async def check_history_bage(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Chỉ dùng được trong server.", ephemeral=True
            )
        target = member or interaction.user
        await interaction.response.defer(ephemeral=True)

        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )
        rows = await asyncio.to_thread(_history_sync, interaction.guild_id, target.id)
        holders = await asyncio.to_thread(_get_holders_sync, interaction.guild_id)
        total = len(holders) or 5

        embed = discord.Embed(
            title=f"🏅 HUY HIỆU · {target.display_name}",
            color=COLOR,
            description=f"Đã có **{len(rows)}/{total}** huy hiệu.",
        )
        if not rows:
            embed.description += "\n\nChưa nhận huy hiệu nào. Dùng `/battle_bage` để bắt đầu."
        else:
            guild = interaction.guild
            for position, awarded_at in rows:
                holder = await _fetch_member(guild, holders[position - 1]) if guild and position <= len(holders) else None
                name = holder.display_name if holder else f"Huy hiệu {position}"
                stamp = int(awarded_at.timestamp())
                embed.add_field(
                    name=f"Huy hiệu {position} · {name}",
                    value=f"<t:{stamp}:F> (<t:{stamp}:R>)",
                    inline=False,
                )
            next_position = len(rows) + 1
            if next_position <= total:
                holder = await _fetch_member(guild, holders[next_position - 1]) if guild and next_position <= len(holders) else None
                target_name = holder.display_name if holder else f"Huy hiệu {next_position}"
                embed.add_field(
                    name="Mục tiêu tiếp theo",
                    value=f"Huy hiệu {next_position} · {target_name}",
                    inline=False,
                )
        embed.set_footer(text="Dark Gym · Shadow Council")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Badge(bot))
