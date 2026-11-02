"""Tính năng đánh lấy huy hiệu (badge) của Dark Gym.

Luồng hoạt động:
- Owner chạy ``/dat_council`` mention tối đa 5 người giữ huy hiệu theo thứ tự từ thấp
  đến cao (4 Shadow Council + Gym Leader, có thể bỏ trống người cuối).
- Member bấm nút trên panel (``/panel_huy_hieu``) hoặc dùng ``/battle_bage``.
- Bot mở **1 phòng** (private thread) chỉ gồm người thách đấu + đúng người giữ huy hiệu
  cần đánh (+ owner), tối đa **2 người xếp hàng chờ**. Phòng gửi kèm thông báo yêu
  cầu chuyển stardust (sửa bằng ``/dat_stardust``).
- Shadow Council / Admin bấm ``✅ Đã đánh xong`` (tính huy hiệu + mở phòng kế tiếp ngay)
  hoặc ``❌ Hủy thách đấu`` (đóng phòng, không tính).
- ``/check_history_bage`` xem huy hiệu đã lấy và ngày giờ.
- ``/reset_huy_hieu`` xoá toàn bộ huy hiệu đã nhận của mọi người.
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
DEFAULT_STARDUST = 50
_UNSET = object()


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
        # Bổ sung cột cho bảng đã tạo từ phiên bản trước.
        conn.execute(
            "ALTER TABLE badge_config "
            "ADD COLUMN IF NOT EXISTS stardust_amount INT NOT NULL DEFAULT 50"
        )
        conn.execute(
            "ALTER TABLE badge_config "
            "ADD COLUMN IF NOT EXISTS stardust_target BIGINT"
        )
        conn.execute(
            "ALTER TABLE badge_config "
            "ADD COLUMN IF NOT EXISTS stardust_note TEXT"
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


def _get_stardust_sync(guild_id):
    """Trả về (số stardust, người nhận, ghi chú) của yêu cầu chuyển stardust."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT stardust_amount, stardust_target, stardust_note "
            "FROM badge_config WHERE guild_id = %s",
            (guild_id,),
        ).fetchone()
    if not row:
        return DEFAULT_STARDUST, None, None
    amount = int(row[0]) if row[0] is not None else DEFAULT_STARDUST
    target = int(row[1]) if row[1] else None
    return amount, target, row[2]


def _set_stardust_sync(guild_id, amount=None, target=None, note=_UNSET):
    """Sửa yêu cầu stardust. Trả về True nếu đã cấu hình huy hiệu trước đó."""
    fields = []
    values = []
    if amount is not None:
        fields.append("stardust_amount = %s")
        values.append(int(amount))
    if target is not None:
        fields.append("stardust_target = %s")
        values.append(int(target))
    if note is not _UNSET:
        fields.append("stardust_note = %s")
        values.append(note)
    if not fields:
        return _get_stardust_sync(guild_id) != (50, None, None)
    values.append(guild_id)
    with get_pool().connection() as conn:
        cursor = conn.execute(
            f"UPDATE badge_config SET {', '.join(fields)} WHERE guild_id = %s",
            values,
        )
        return cursor.rowcount > 0


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
            return "queued", {
                "position": int(waiting) + 1,
                "badge": position,
                "holder": holders[position - 1],
            }

        room_id = conn.execute(
            "INSERT INTO badge_rooms (guild_id, user_id, position) "
            "VALUES (%s, %s, %s) RETURNING id",
            (guild_id, user_id, position),
        ).fetchone()[0]
        return "room", {
            "room_id": int(room_id),
            "position": position,
            "holder": holders[position - 1],
        }


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

        hrow = conn.execute(
            "SELECT holder_ids FROM badge_config WHERE guild_id = %s", (guild_id,)
        ).fetchone()
        holders = [int(x) for x in hrow[0]] if hrow else []

        def _holder_at(index):
            return holders[index - 1] if 0 < index <= len(holders) else None

        holder_id = _holder_at(position)

        if won:
            conn.execute(
                "INSERT INTO badge_history (guild_id, user_id, position) "
                "VALUES (%s, %s, %s)",
                (guild_id, user_id, position),
            )
        conn.execute("UPDATE badge_rooms SET status = 'closed' WHERE id = %s", (room_id,))

        next_user = None
        next_room = None
        next_position = None
        next_holder = None
        advanced = False

        # Thắng và còn người kế tiếp: mở phòng luôn cho chính người vừa thắng.
        if won and _holder_at(position + 1) is not None:
            next_user = user_id
            next_position = position + 1
            next_holder = _holder_at(next_position)
            next_room = int(
                conn.execute(
                    "INSERT INTO badge_rooms (guild_id, user_id, position) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (guild_id, next_user, next_position),
                ).fetchone()[0]
            )
            advanced = True

        queue = conn.execute(
            "SELECT user_id FROM badge_queue WHERE guild_id = %s ORDER BY created_at",
            (guild_id,),
        ).fetchall()
        for row_q in queue:
            if advanced:
                break
            candidate = int(row_q[0])
            conn.execute(
                "DELETE FROM badge_queue WHERE guild_id = %s AND user_id = %s",
                (guild_id, candidate),
            )
            earned = conn.execute(
                "SELECT COUNT(*) FROM badge_history "
                "WHERE guild_id = %s AND user_id = %s",
                (guild_id, candidate),
            ).fetchone()[0]
            candidate_position = int(earned) + 1
            candidate_holder = _holder_at(candidate_position)
            if candidate_holder is None:
                # Người chờ đã lấy đủ huy hiệu (cấu hình vừa đổi): bỏ qua.
                continue
            next_user = candidate
            next_position = candidate_position
            next_holder = candidate_holder
            next_room = int(
                conn.execute(
                    "INSERT INTO badge_rooms (guild_id, user_id, position) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (guild_id, next_user, next_position),
                ).fetchone()[0]
            )
            break

    return "ok", {
        "guild_id": guild_id,
        "user_id": user_id,
        "position": position,
        "holder_id": holder_id,
        "thread_id": int(thread_id) if thread_id else None,
        "won": bool(won),
        "next_user": next_user,
        "next_room": next_room,
        "next_position": next_position,
        "next_holder": next_holder,
        "advanced": advanced,
    }


def _set_thread_sync(room_id, thread_id):
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE badge_rooms SET thread_id = %s WHERE id = %s",
            (thread_id, room_id),
        )


def _reset_badges_sync(guild_id):
    """Xóa huy hiệu đã nhận của mọi người, đóng phòng đang mở và xóa hàng đợi.

    Trả về (số huy hiệu đã xóa, danh sách thread_id cần khoá).
    """
    with get_pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (guild_id,))
        removed = conn.execute(
            "DELETE FROM badge_history WHERE guild_id = %s", (guild_id,)
        ).rowcount
        conn.execute("DELETE FROM badge_queue WHERE guild_id = %s", (guild_id,))
        threads = conn.execute(
            "SELECT thread_id FROM badge_rooms "
            "WHERE guild_id = %s AND status = 'open' AND thread_id IS NOT NULL",
            (guild_id,),
        ).fetchall()
        conn.execute(
            "UPDATE badge_rooms SET status = 'closed' "
            "WHERE guild_id = %s AND status = 'open'",
            (guild_id,),
        )
    return int(removed or 0), [int(row[0]) for row in threads]


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


def _stardust_text(amount, target, note):
    """Nội dung yêu cầu chuyển stardust. `note` thay thế toàn bộ câu mặc định."""
    if note:
        return note
    if target:
        return f"**Yêu cầu:** chuyển **{amount} stardust** cho <@{target}> trước khi đánh."
    return f"**Yêu cầu:** chuyển **{amount} stardust** cho Gym Leader trước khi đánh."


def _room_embed(challenger, holder, stardust_text):
    embed = discord.Embed(
        title=f"🏅 THÁCH ĐẤU · {holder.display_name}",
        color=COLOR,
        description=(
            f"{challenger.mention} thách đấu {holder.mention} để lấy huy hiệu.\n\n"
            f"{stardust_text}\n\n"
            "Đánh xong, **Shadow Council** hoặc **Admin** bấm bên dưới:\n"
            "`✅ Đã đánh xong` = tính huy hiệu và mở phòng kế tiếp · "
            "`❌ Hủy thách đấu` = đóng phòng, không tính."
        ),
    )
    embed.set_footer(text="Dark Gym · Shadow Council")
    return embed


def _reset_view(step):
    """Bước 1 và bước 2 của lệnh /reset_huy_hieu (xác nhận 2 lần)."""
    view = discord.ui.View(timeout=120)
    if step == 1:
        view.add_item(
            discord.ui.Button(
                label="Tôi chắc chắn (1/2)",
                style=discord.ButtonStyle.danger,
                custom_id="badge:reset:1",
            )
        )
    else:
        view.add_item(
            discord.ui.Button(
                label="XÓA HẾT HUY HIỆU (2/2)",
                style=discord.ButtonStyle.danger,
                custom_id="badge:reset:2",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Thôi, giữ nguyên",
                style=discord.ButtonStyle.secondary,
                custom_id="badge:reset:cancel",
            )
        )
    return view


def _room_view(room_id):
    """Nút trong phòng. Callback rỗng: xử lý thật nằm ở `on_interaction` để nút
    vẫn hoạt động sau khi bot restart (view không còn trong bộ nhớ).
    """
    view = discord.ui.View(timeout=None)
    for action, label, emoji, style in (
        ("done", "Đã đánh xong", "✅", discord.ButtonStyle.success),
        ("cancel", "Hủy thách đấu", "❌", discord.ButtonStyle.danger),
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
    if holder is None:
        raise RuntimeError(f"Không tìm thấy người giữ huy hiệu {holder_id} trong server.")

    thread = await parent.create_thread(
        name=f"🏅 {challenger.display_name} vs {holder.display_name}"[:95],
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=1440,
    )

    invited = [challenger, holder]
    owner = await _fetch_member(guild, OWNER_ID)
    if owner is not None and owner not in invited:
        invited.append(owner)

    for member in invited:
        try:
            await thread.add_user(member)
        except discord.HTTPException:
            log.warning("Không thêm được %s vào phòng huy hiệu", member.id)

    await _set_thread_sync(room_id, thread.id)

    amount, target, note = await asyncio.to_thread(_get_stardust_sync, guild.id)
    if target is None and holders:
        target = holders[-1]
    stardust = _stardust_text(amount, target, note)

    mentions = " ".join(m.mention for m in invited)
    embed = _room_embed(challenger, holder, stardust)
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
            f"Bạn đã lấy đủ huy hiệu mùa này. Không còn gì để chinh phục!",
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
            f"(sẽ thách đấu <@{payload['holder']}>).\n"
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
        f"Đã mở phòng cho bạn: {thread.mention} — thách đấu <@{payload['holder']}>.",
        ephemeral=True,
    )


async def _promote_next(client, guild, payload):
    """Mở phòng kế tiếp (người vừa thắng leo tiếp, hoặc người đầu hàng đợi)."""
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
        try:
            if custom_id.startswith("badge:done:") or custom_id.startswith("badge:cancel:"):
                _, action, raw_id = custom_id.split(":")
                return await self._on_result(interaction, int(raw_id), action == "done")
            # Tương thích nút của phòng tạo trước khi đổi tên.
            if custom_id.startswith("badge:win:") or custom_id.startswith("badge:lose:"):
                _, action, raw_id = custom_id.split(":")
                return await self._on_result(interaction, int(raw_id), action == "win")
            if custom_id == "badge:reset:1":
                return await self._on_reset(interaction, 1)
            if custom_id == "badge:reset:2":
                return await self._on_reset(interaction, 2)
            if custom_id == "badge:reset:cancel":
                return await interaction.response.edit_message(
                    content="Đã hủy. Huy hiệu của mọi người được giữ nguyên.",
                    embed=None,
                    view=None,
                )
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
            holder = (
                f"<@{payload['holder_id']}>"
                if payload.get("holder_id")
                else "đối thủ"
            )
            if payload["won"]:
                text = (
                    f"🏅 <@{payload['user_id']}> đã vượt qua {holder} và lấy được huy hiệu!"
                )
            else:
                text = (
                    f"❌ <@{payload['user_id']}> đã hủy thách đấu với {holder}."
                )
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                log.warning("Không gửi được thông báo kết quả huy hiệu")

        next_thread = await _promote_next(self.bot, guild, payload)
        if next_thread is not None and channel is not None:
            if payload.get("advanced"):
                text = (
                    f"➡️ <@{payload['next_user']}> tiếp tục thách đấu "
                    f"<@{payload['next_holder']}> {next_thread.mention}"
                )
            else:
                text = (
                    f"➡️ Phòng tiếp theo: <@{payload['next_user']}> vs "
                    f"<@{payload['next_holder']}> {next_thread.mention}"
                )
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass

        if payload.get("won") and not payload.get("next_room"):
            await interaction.followup.send(
                "Đã tính huy hiệu cuối cùng — người này đã lấy đủ bộ huy hiệu mùa này!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Đã ghi nhận kết quả.", ephemeral=True)

    # ------------------------------------------------------------------ #
    # Reset huy hiệu (xác nhận 2 lần)
    # ------------------------------------------------------------------ #

    async def _on_reset(self, interaction, step):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Chỉ dùng được trong server.", ephemeral=True
            )

        if step == 1:
            embed = discord.Embed(
                title="⚠️ XÓA TOÀN BỘ HUY HIỆU (bước 1/2)",
                description=(
                    "Lệnh này xóa **tất cả huy hiệu đã nhận của mọi người** trong mùa này, "
                    "xóa hàng đợi và đóng các phòng đang mở.\n\n"
                    "Danh sách người giữ huy hiệu (`/dat_council`) **vẫn được giữ**.\n\n"
                    "**Không thể hoàn tác.** Bấm nút bên dưới để xác nhận lần 2."
                ),
                color=0xE74C3C,
            )
            return await interaction.response.edit_message(
                embed=embed, view=_reset_view(2)
            )

        await interaction.response.defer(ephemeral=True)
        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )

        removed, thread_ids = await asyncio.to_thread(_reset_badges_sync, guild.id)

        # Khoá các phòng đang mở để không ai đánh tiếp trên dữ liệu vừa xoá.
        for thread_id in thread_ids:
            thread = await get_channel(self.bot, thread_id)
            if not isinstance(thread, discord.Thread):
                continue
            try:
                await thread.send("🔒 Huy hiệu mùa này đã được reset. Phòng đóng lại.")
            except discord.HTTPException:
                pass
            try:
                await thread.edit(locked=True, archived=True)
            except discord.HTTPException:
                log.warning("Không khoá được phòng huy hiệu %s", thread_id)

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="✅ ĐÃ RESET HUY HIỆU",
                description=(
                    f"Đã xóa **{removed}** huy hiệu đã nhận, xóa hàng đợi và đóng "
                    f"**{len(thread_ids)}** phòng đang mở.\n"
                    "Mọi người bắt đầu lại từ người giữ huy hiệu đầu tiên."
                ),
                color=COLOR,
            ),
            view=None,
        )

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
        description="[OWNER] Đặt thứ tự người giữ huy hiệu từ thấp đến cao.",
    )
    @app_commands.describe(
        nguoi_1="Người đầu tiên (thấp nhất)",
        nguoi_2="Người thứ 2",
        nguoi_3="Người thứ 3",
        nguoi_4="Người thứ 4",
        nguoi_5="Người cuối cùng (cao nhất, thường là Gym Leader) — có thể để trống",
    )
    async def dat_council(
        self,
        interaction: discord.Interaction,
        nguoi_1: discord.Member,
        nguoi_2: discord.Member,
        nguoi_3: discord.Member,
        nguoi_4: discord.Member,
        nguoi_5: discord.Member | None = None,
    ):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        members = [m for m in (nguoi_1, nguoi_2, nguoi_3, nguoi_4, nguoi_5) if m]
        if len({m.id for m in members}) != len(members):
            return await interaction.response.send_message(
                "Những người được chọn phải khác nhau.", ephemeral=True
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
        lines = "\n".join(f"• {m.mention}" for m in members)
        if len(members) < 5:
            lines += "\n• (chưa đặt người cuối)"
        await interaction.followup.send(
            f"Đã đặt thứ tự thách đấu:\n{lines}", ephemeral=True
        )

    @app_commands.command(
        name="dat_stardust",
        description="[OWNER] Xem/sửa yêu cầu chuyển stardust khi mở phòng huy hiệu.",
    )
    @app_commands.describe(
        so_tien="Số stardust yêu cầu (bỏ trống để xem cấu hình hiện tại)",
        nguoi_nhan="Người nhận stardust (mặc định là người cuối trong /dat_council)",
        noi_dung='Câu thông báo thay thế hoàn toàn (gõ "-" để xoá, bỏ trống để giữ nguyên)',
    )
    async def dat_stardust(
        self,
        interaction: discord.Interaction,
        so_tien: int | None = None,
        nguoi_nhan: discord.Member | None = None,
        noi_dung: str | None = None,
    ):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        if so_tien is not None and so_tien < 0:
            return await interaction.response.send_message(
                "Số stardust phải là số không âm.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )

        note = _UNSET
        if noi_dung is not None:
            note = None if noi_dung.strip() == "-" else noi_dung.strip()

        if so_tien is None and nguoi_nhan is None and note is _UNSET:
            amount, target, note_now = await asyncio.to_thread(
                _get_stardust_sync, interaction.guild_id
            )
            if target is None:
                holders = await asyncio.to_thread(
                    _get_holders_sync, interaction.guild_id
                )
                target = holders[-1] if holders else None
            target_text = f"<@{target}>" if target else "(chưa đặt)"
            return await interaction.followup.send(
                f"**Stardust hiện tại:** {amount}\n"
                f"**Người nhận:** {target_text}\n"
                f"**Câu thông báo:** {note_now or '(mặc định)'}",
                ephemeral=True,
            )

        updated = await asyncio.to_thread(
            _set_stardust_sync,
            interaction.guild_id,
            so_tien,
            nguoi_nhan.id if nguoi_nhan else None,
            note,
        )
        if not updated:
            return await interaction.followup.send(
                "Chưa có cấu hình huy hiệu. Chạy `/dat_council` trước.", ephemeral=True
            )

        amount, target, note_now = await asyncio.to_thread(
            _get_stardust_sync, interaction.guild_id
        )
        if target is None:
            holders = await asyncio.to_thread(_get_holders_sync, interaction.guild_id)
            target = holders[-1] if holders else None
        await interaction.followup.send(
            "Đã cập nhật thông báo stardust:\n"
            + _stardust_text(amount, target, note_now),
            ephemeral=True,
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
        for holder_id in holders:
            member = await _fetch_member(guild, holder_id) if guild else None
            name = member.mention if member else f"<@{holder_id}>"
            lines.append(f"• {name}")

        amount, target, note = await asyncio.to_thread(
            _get_stardust_sync, interaction.guild_id
        )
        if target is None and holders:
            target = holders[-1]
        stardust = _stardust_text(amount, target, note)

        embed = discord.Embed(
            title="🏅 THÁCH ĐẤU LẤY HUY HIỆU",
            color=COLOR,
            description=(
                "Lần lượt vượt qua từng người giữ huy hiệu để lấy đủ bộ huy hiệu mùa này.\n\n"
                + "\n".join(lines)
                + f"\n\n{stardust}\n\n"
                + "**Cách tham gia:** bấm nút bên dưới hoặc dùng `/battle_bage`.\n"
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
        description="Xem huy hiệu đã lấy mùa này và ngày giờ nhận.",
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
        guild = interaction.guild

        embed = discord.Embed(
            title=f"🏅 HUY HIỆU · {target.display_name}",
            color=COLOR,
            description="Đã lấy huy hiệu mùa này",
        )

        for position, awarded_at in rows:
            name = "?"
            if position <= len(holders):
                holder = (
                    await _fetch_member(guild, holders[position - 1]) if guild else None
                )
                name = holder.display_name if holder else f"<@{holders[position - 1]}>"
            stamp = int(awarded_at.timestamp())
            embed.add_field(
                name=name,
                value=f"<t:{stamp}:F> (<t:{stamp}:R>)",
                inline=False,
            )

        next_position = len(rows) + 1
        if next_position <= len(holders):
            nxt = await _fetch_member(guild, holders[next_position - 1]) if guild else None
            next_name = nxt.mention if nxt else f"<@{holders[next_position - 1]}>"
            embed.add_field(
                name="Cần thách đấu với",
                value=next_name,
                inline=False,
            )
        else:
            embed.add_field(
                name="Cần thách đấu với",
                value="Đã lấy đủ huy hiệu mùa này 🎉",
                inline=False,
            )

        embed.set_footer(text="Dark Gym · Shadow Council")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="reset_huy_hieu",
        description="[OWNER] Xóa toàn bộ huy hiệu đã nhận của mọi người (xác nhận 2 lần).",
    )
    async def reset_huy_hieu(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        embed = discord.Embed(
            title="⚠️ XÓA TOÀN BỘ HUY HIỆU (bước 1/2)",
            description=(
                "Lệnh này xóa **tất cả huy hiệu đã nhận của mọi người** trong mùa này, "
                "xóa hàng đợi và đóng các phòng đang mở.\n\n"
                "Danh sách người giữ huy hiệu (`/dat_council`) **vẫn được giữ**.\n\n"
                "**Không thể hoàn tác.** Bấm nút bên dưới để bắt đầu xác nhận."
            ),
            color=0xE74C3C,
        )
        await interaction.response.send_message(
            embed=embed, view=_reset_view(1), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Badge(bot))
