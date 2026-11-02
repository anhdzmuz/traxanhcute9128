"""Tính năng đánh lấy huy hiệu (badge) của Dark Gym.

Luồng hoạt động:
- Owner chạy ``/dat_council`` mention danh sách Shadow Council (tối đa 10 người).
- Member bấm nút trên panel (``/panel_huy_hieu``) hoặc dùng ``/battle_bage``.
- Bot mở **1 phòng** (private thread) gồm người thách đấu + Shadow Council + owner.
  Bot **không** can thiệp vào trận: người thách đấu tự vào game đánh với council.
- Phòng gửi kèm thông báo yêu cầu chuyển stardust (sửa bằng ``/dat_stardust``).
- 3 nút trong phòng:
  ``✅ Đã thách đấu council xong`` (người thách đấu bấm) →
  ``✔️ Xác nhận`` (council/owner bấm) → bot chỉ tag owner để owner tự mở phòng
  Gym Leader. ``❌ Hủy thách đấu`` đóng phòng, không tính.
- Chỉ người thắng council mới được đánh Gym Leader. Thắng Gym Leader xong owner
  chạy ``/trao_huy_hieu`` để ghi nhận huy hiệu.
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
    resolve_thread_parent,
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


def _council_ids(holders):
    """Toàn bộ Shadow Council đã đặt (tối đa 10 người)."""
    return list(holders)


def _stardust_target(stored):
    """Người nhận stardust: cấu hình /dat_stardust, mặc định là owner."""
    return int(stored) if stored else OWNER_ID


def _badge_sync(guild_id, user_id):
    """Trả về thời điểm nhận huy hiệu mùa này, hoặc None nếu chưa có."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT awarded_at FROM badge_history "
            "WHERE guild_id = %s AND user_id = %s "
            "ORDER BY awarded_at DESC LIMIT 1",
            (guild_id, user_id),
        ).fetchone()
    return row[0] if row else None


def _award_badge_sync(guild_id, user_id):
    """Trao huy hiệu mùa này. Trả về 'ok' / 'holder' / 'already'."""
    with get_pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (guild_id,))
        row = conn.execute(
            "SELECT holder_ids FROM badge_config WHERE guild_id = %s", (guild_id,)
        ).fetchone()
        holders = [int(x) for x in row[0]] if row else []
        if int(user_id) in holders:
            return "holder"
        if conn.execute(
            "SELECT 1 FROM badge_history WHERE guild_id = %s AND user_id = %s",
            (guild_id, user_id),
        ).fetchone():
            return "already"
        conn.execute(
            "INSERT INTO badge_history (guild_id, user_id, position) "
            "VALUES (%s, %s, 1)",
            (guild_id, user_id),
        )
    return "ok"


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
        return _get_stardust_sync(guild_id) != (DEFAULT_STARDUST, None, None)
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

        if conn.execute(
            "SELECT 1 FROM badge_history WHERE guild_id = %s AND user_id = %s",
            (guild_id, user_id),
        ).fetchone():
            return "done", {}

        if conn.execute(
            "SELECT 1 FROM badge_rooms "
            "WHERE guild_id = %s AND user_id = %s "
            "AND status IN ('open', 'awaiting')",
            (guild_id, user_id),
        ).fetchone():
            return "in_room", {}

        if conn.execute(
            "SELECT 1 FROM badge_queue WHERE guild_id = %s AND user_id = %s",
            (guild_id, user_id),
        ).fetchone():
            return "in_queue", {"position": _queue_index(conn, guild_id, user_id)}

        busy = conn.execute(
            "SELECT 1 FROM badge_rooms "
            "WHERE guild_id = %s AND status IN ('open', 'awaiting') LIMIT 1",
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
            return "queued", {"position": int(waiting) + 1}

        room_id = conn.execute(
            "INSERT INTO badge_rooms (guild_id, user_id, position) "
            "VALUES (%s, %s, 1) RETURNING id",
            (guild_id, user_id),
        ).fetchone()[0]
        return "room", {"room_id": int(room_id)}


def _room_info_sync(room_id):
    """Thông tin cơ bản của phòng, hoặc None nếu không tồn tại."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT guild_id, user_id, thread_id, status "
            "FROM badge_rooms WHERE id = %s",
            (room_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "guild_id": int(row[0]),
        "user_id": int(row[1]),
        "thread_id": int(row[2]) if row[2] else None,
        "status": row[3],
    }


def _room_action_sync(room_id, action):
    """Chuyển trạng thái phòng theo nút bấm.

    ``done``    : open     -> awaiting  (người thách đấu báo đã đánh xong council)
    ``confirm`` : awaiting -> confirmed (council/owner xác nhận, nhường slot)
    ``cancel``  : open/awaiting -> closed (huỷ, nhường slot)
    """
    with get_pool().connection() as conn:
        first = conn.execute(
            "SELECT guild_id FROM badge_rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if not first:
            return "gone", {}
        guild_id = int(first[0])
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (guild_id,))

        row = conn.execute(
            "SELECT user_id, thread_id, status FROM badge_rooms "
            "WHERE id = %s FOR UPDATE",
            (room_id,),
        ).fetchone()
        if not row:
            return "gone", {}
        user_id = int(row[0])
        thread_id = int(row[1]) if row[1] else None
        current = row[2]

        if action == "done":
            if current != "open":
                return "bad_state", {"status": current}
            conn.execute(
                "UPDATE badge_rooms SET status = 'awaiting' WHERE id = %s", (room_id,)
            )
            new_status = "awaiting"
            next_user = None
            next_room = None
        elif action in ("confirm", "cancel"):
            if current not in ("open", "awaiting"):
                return "bad_state", {"status": current}
            new_status = "confirmed" if action == "confirm" else "closed"
            conn.execute(
                "UPDATE badge_rooms SET status = %s WHERE id = %s",
                (new_status, room_id),
            )
            next_user = None
            next_room = None
            queue = conn.execute(
                "SELECT user_id FROM badge_queue WHERE guild_id = %s ORDER BY created_at",
                (guild_id,),
            ).fetchall()
            for row_q in queue:
                candidate = int(row_q[0])
                conn.execute(
                    "DELETE FROM badge_queue WHERE guild_id = %s AND user_id = %s",
                    (guild_id, candidate),
                )
                if conn.execute(
                    "SELECT 1 FROM badge_history "
                    "WHERE guild_id = %s AND user_id = %s",
                    (guild_id, candidate),
                ).fetchone():
                    # Người chờ đã có huy hiệu: bỏ qua.
                    continue
                next_user = candidate
                next_room = int(
                    conn.execute(
                        "INSERT INTO badge_rooms (guild_id, user_id, position) "
                        "VALUES (%s, %s, 1) RETURNING id",
                        (guild_id, candidate),
                    ).fetchone()[0]
                )
                break
        else:
            return "bad_action", {}

    return "ok", {
        "guild_id": guild_id,
        "user_id": user_id,
        "thread_id": thread_id,
        "status": new_status,
        "next_user": next_user,
        "next_room": next_room,
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
            "WHERE guild_id = %s AND status IN ('open', 'awaiting') "
            "AND thread_id IS NOT NULL",
            (guild_id,),
        ).fetchall()
        conn.execute(
            "UPDATE badge_rooms SET status = 'closed' "
            "WHERE guild_id = %s AND status IN ('open', 'awaiting')",
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
                "WHERE guild_id = %s AND status IN ('open', 'awaiting')",
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


def _room_embed(challenger, council, stardust_text):
    names = ", ".join(member.mention for member in council) or "Shadow Council"
    embed = discord.Embed(
        title="🏅 THÁCH ĐẤU SHADOW COUNCIL",
        color=COLOR,
        description=(
            f"{challenger.mention} thách đấu: {names}\n\n"
            f"{stardust_text}\n\n"
            "**Cách làm:**\n"
            "1️⃣ Tự vào game thách đấu lần lượt các Shadow Council.\n"
            "2️⃣ Thắng council xong, bấm **✅ Đã thách đấu council xong**.\n"
            "3️⃣ Council hoặc Gym Leader bấm **✔️ Xác nhận** để mở phòng Gym Leader.\n\n"
            "Chỉ người thắng council mới được đánh Gym Leader, và "
            "**thắng Gym Leader mới có huy hiệu**."
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


def _room_view(room_id, stage="open"):
    """Nút trong phòng. Callback rỗng: xử lý thật nằm ở `on_interaction` để nút
    vẫn hoạt động sau khi bot restart (view không còn trong bộ nhớ).

    ``stage="open"``    : đã đánh xong council (bấm được) + xác nhận (khoá) + huỷ.
    ``stage="awaiting"``: đã đánh xong (khoá) + xác nhận (bấm được) + huỷ.
    """
    view = discord.ui.View(timeout=None)
    for action, label, emoji, style, enabled in (
        (
            "done",
            "Đã thách đấu council xong",
            "✅",
            discord.ButtonStyle.primary,
            stage == "open",
        ),
        (
            "confirm",
            "Xác nhận",
            "✔️",
            discord.ButtonStyle.success,
            stage == "awaiting",
        ),
        (
            "cancel",
            "Hủy thách đấu",
            "❌",
            discord.ButtonStyle.danger,
            True,
        ),
    ):
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"badge:{action}:{room_id}",
            disabled=not enabled,
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


async def open_room(client, guild, room_id, user_id, holders):
    """Tạo private thread cho một phòng đã ghi trong DB. Trả về thread.

    Phòng gồm: người thách đấu + Shadow Council + Gym Leader/owner.
    Bot chỉ tạo phòng, không can thiệp vào các trận.
    """
    parent, kind = await resolve_thread_parent(client, MATCH_CHANNEL_ID)

    challenger = await _fetch_member(guild, user_id)
    if challenger is None:
        raise RuntimeError(f"Không tìm thấy member {user_id} trong server.")

    council = []
    for holder_id in _council_ids(holders):
        member = await _fetch_member(guild, holder_id)
        if member is not None and member not in council:
            council.append(member)
    if not council:
        raise RuntimeError("Chưa có Shadow Council, hãy chạy /dat_council lại.")

    invited = [challenger, *council]
    owner = await _fetch_member(guild, OWNER_ID)
    if owner is not None and owner not in invited:
        invited.append(owner)

    amount, target, note = await asyncio.to_thread(_get_stardust_sync, guild.id)
    target = _stardust_target(target)
    stardust = _stardust_text(amount, target, note)

    mentions = " ".join(m.mention for m in invited)
    embed = _room_embed(challenger, council, stardust)
    view = _room_view(room_id)
    name = f"🏅 {challenger.display_name} vs Shadow Council"[:95]

    if kind == "forum":
        # Forum không hỗ trợ private thread; phòng sẽ là bài đăng công khai.
        log.warning(
            "Kênh trận %s là forum — phòng huy hiệu sẽ công khai, "
            "nên dùng Text Channel nếu cần riêng tư.",
            MATCH_CHANNEL_ID,
        )
        created = await parent.create_thread(
            name=name,
            auto_archive_duration=1440,
            content=mentions,
            embed=embed,
            view=view,
        )
        thread = created.thread
    else:
        thread = await parent.create_thread(
            name=name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
        )
        for member in invited:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                log.warning("Không thêm được %s vào phòng huy hiệu", member.id)
        await thread.send(content=mentions, embed=embed, view=view)

    await asyncio.to_thread(_set_thread_sync, room_id, thread.id)
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
            "Bạn là Shadow Council / Gym Leader nên không thể thách đấu.", ephemeral=True
        )
    if status == "done":
        return await interaction.followup.send(
            "Bạn đã lấy huy hiệu mùa này rồi.", ephemeral=True
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
            f"Đã vào hàng đợi ở vị trí **{payload['position']}/{MAX_QUEUE}**.\n"
            "Khi phòng hiện tại xong, bot sẽ mở phòng cho bạn.",
            ephemeral=True,
        )

    # status == "room"
    holders = await asyncio.to_thread(_get_holders_sync, guild.id)
    try:
        thread = await open_room(
            interaction.client, guild, payload["room_id"], interaction.user.id, holders
        )
    except Exception as exc:  # noqa: BLE001 - dọn phòng để không kẹt slot
        log.exception("Không tạo được phòng huy hiệu")
        try:
            result = await asyncio.to_thread(
                _room_action_sync, payload["room_id"], "cancel"
            )
            await _promote_next(interaction.client, guild, result[1])
        except Exception:
            log.exception("Không dọn được phòng huy hiệu lỗi")
        return await interaction.followup.send(
            f"Không tạo được phòng: `{type(exc).__name__}: {str(exc)[:200]}`",
            ephemeral=True,
        )

    await interaction.followup.send(
        f"Đã mở phòng cho bạn: {thread.mention} — tự vào game thách đấu các Shadow Council.",
        ephemeral=True,
    )


async def _promote_next(client, guild, payload):
    """Mở phòng cho người đầu hàng đợi (nếu có)."""
    if not payload or not payload.get("next_room"):
        return None
    holders = await asyncio.to_thread(_get_holders_sync, guild.id)
    try:
        return await open_room(
            client, guild, payload["next_room"], payload["next_user"], holders
        )
    except Exception:  # noqa: BLE001
        log.exception("Không mở được phòng cho người kế tiếp")
        # Đóng phòng vừa tạo để nhường slot cho người sau trong hàng đợi.
        result = await asyncio.to_thread(_room_action_sync, payload["next_room"], "cancel")
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
            if custom_id.startswith("badge:done:"):
                _, _, raw_id = custom_id.split(":")
                return await self._on_room_action(interaction, int(raw_id), "done")
            if custom_id.startswith("badge:confirm:"):
                _, _, raw_id = custom_id.split(":")
                return await self._on_room_action(interaction, int(raw_id), "confirm")
            if custom_id.startswith("badge:cancel:"):
                _, _, raw_id = custom_id.split(":")
                return await self._on_room_action(interaction, int(raw_id), "cancel")
            # Tương thích nút của phòng tạo trước khi đổi tên.
            if custom_id.startswith("badge:win:"):
                _, _, raw_id = custom_id.split(":")
                return await self._on_room_action(interaction, int(raw_id), "confirm")
            if custom_id.startswith("badge:lose:"):
                _, _, raw_id = custom_id.split(":")
                return await self._on_room_action(interaction, int(raw_id), "cancel")
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

    async def _on_room_action(self, interaction, room_id, action):
        """Xử lý 3 nút trong phòng huy hiệu.

        - ``done``    : chỉ người thách đấu bấm (đã đánh xong council).
        - ``confirm`` : council/owner xác nhận, bot chỉ tag owner mở phòng Leader.
        - ``cancel``  : người thách đấu hoặc council/owner huỷ phòng.
        """
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Chỉ dùng được trong server.", ephemeral=True
            )

        info = await asyncio.to_thread(_room_info_sync, room_id)
        if info is None:
            return await interaction.response.send_message(
                "Phòng này không còn tồn tại.", ephemeral=True
            )

        holders = await asyncio.to_thread(_get_holders_sync, guild.id)
        is_challenger = interaction.user.id == info["user_id"]
        is_judge = can_judge(interaction.user, holders)

        if action == "done" and not is_challenger:
            return await interaction.response.send_message(
                "Chỉ người thách đấu mới bấm được nút này.", ephemeral=True
            )
        if action == "confirm" and not is_judge:
            return await interaction.response.send_message(
                "Chỉ **Shadow Council** hoặc **Gym Leader** được xác nhận.",
                ephemeral=True,
            )
        if action == "cancel" and not (is_challenger or is_judge):
            return await interaction.response.send_message(
                "Chỉ người thách đấu, council hoặc Gym Leader được hủy.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )

        status, payload = await asyncio.to_thread(_room_action_sync, room_id, action)
        if status == "gone":
            return await interaction.followup.send(
                "Phòng này đã được xử lý rồi.", ephemeral=True
            )
        if status == "bad_state":
            if action == "done":
                return await interaction.followup.send(
                    "Phòng này đã được xác nhận hoặc đã hủy rồi.", ephemeral=True
                )
            return await interaction.followup.send(
                "Người thách đấu chưa bấm **Đã thách đấu council xong**.",
                ephemeral=True,
            )

        thread = (
            await get_channel(self.bot, payload["thread_id"])
            if payload.get("thread_id")
            else None
        )
        channel = await get_channel(self.bot, GYM_CHANNEL_ID)

        if action == "done":
            if isinstance(thread, discord.Thread):
                try:
                    await thread.send(
                        f"⏳ <@{payload['user_id']}> đã đánh xong council.\n"
                        f"<@{OWNER_ID}> hoặc council bấm **✔️ Xác nhận** để mở phòng "
                        "Gym Leader.",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.HTTPException:
                    pass
            try:
                await interaction.message.edit(view=_room_view(room_id, "awaiting"))
            except discord.HTTPException:
                log.warning("Không cập nhật được nút phòng huy hiệu %s", room_id)
            return await interaction.followup.send(
                "Đã ghi nhận. Chờ council hoặc Gym Leader bấm **Xác nhận**.",
                ephemeral=True,
            )

        # confirm / cancel: đóng phòng và nhường slot.
        if isinstance(thread, discord.Thread):
            try:
                if action == "confirm":
                    await thread.send(
                        f"✅ Council đã xác nhận. <@{payload['user_id']}> được đánh "
                        "Gym Leader.\nThắng Gym Leader mới được trao huy hiệu."
                    )
                else:
                    await thread.send("❌ Đã hủy thách đấu huy hiệu.")
            except discord.HTTPException:
                pass
            try:
                await thread.edit(locked=True, archived=True)
            except discord.HTTPException:
                log.warning("Không khoá được phòng huy hiệu %s", payload.get("thread_id"))

        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass

        if channel is not None:
            if action == "confirm":
                text = (
                    f"🏅 <@{payload['user_id']}> đã vượt qua Shadow Council!\n"
                    f"<@{OWNER_ID}> mở phòng Gym Leader cho <@{payload['user_id']}> nhé."
                )
            else:
                text = f"❌ <@{payload['user_id']}> đã hủy thách đấu huy hiệu."
            try:
                await channel.send(
                    text, allowed_mentions=discord.AllowedMentions(users=True)
                )
            except discord.HTTPException:
                log.warning("Không gửi được thông báo kết quả huy hiệu")

        next_thread = await _promote_next(self.bot, guild, payload)
        if next_thread is not None and channel is not None:
            try:
                await channel.send(
                    f"➡️ Phòng tiếp theo: <@{payload['next_user']}> vs Shadow Council "
                    f"{next_thread.mention}",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                pass

        if action == "confirm":
            await interaction.followup.send(
                "Đã xác nhận. Bot đã tag Gym Leader để mở phòng — thắng Gym Leader "
                "mới có huy hiệu.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Đã hủy thách đấu.", ephemeral=True)

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
                    "Mọi người có thể đăng ký thách đấu lại từ đầu."
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
                        result = await asyncio.to_thread(
                            _room_action_sync, room_id, "cancel"
                        )
                        if result[0] == "ok":
                            await _promote_next(self.bot, guild, result[1])
                        continue
                    thread = await get_channel(self.bot, thread_id)
                    if isinstance(thread, discord.Thread):
                        continue
                    log.warning("Phòng huy hiệu %s mất thread, đóng để nhường slot", room_id)
                    result = await asyncio.to_thread(_room_action_sync, room_id, "cancel")
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
        description="[OWNER] Đặt danh sách Shadow Council (tối đa 10 người).",
    )
    @app_commands.describe(
        nguoi_1="Shadow Council thứ 1",
        nguoi_2="Shadow Council thứ 2",
        nguoi_3="Shadow Council thứ 3",
        nguoi_4="Shadow Council thứ 4",
        nguoi_5="Shadow Council thứ 5 (có thể để trống)",
        nguoi_6="Shadow Council thứ 6 (có thể để trống)",
        nguoi_7="Shadow Council thứ 7 (có thể để trống)",
        nguoi_8="Shadow Council thứ 8 (có thể để trống)",
        nguoi_9="Shadow Council thứ 9 (có thể để trống)",
        nguoi_10="Shadow Council thứ 10 (có thể để trống)",
    )
    async def dat_council(
        self,
        interaction: discord.Interaction,
        nguoi_1: discord.Member,
        nguoi_2: discord.Member,
        nguoi_3: discord.Member,
        nguoi_4: discord.Member,
        nguoi_5: discord.Member | None = None,
        nguoi_6: discord.Member | None = None,
        nguoi_7: discord.Member | None = None,
        nguoi_8: discord.Member | None = None,
        nguoi_9: discord.Member | None = None,
        nguoi_10: discord.Member | None = None,
    ):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        members = [
            m
            for m in (
                nguoi_1,
                nguoi_2,
                nguoi_3,
                nguoi_4,
                nguoi_5,
                nguoi_6,
                nguoi_7,
                nguoi_8,
                nguoi_9,
                nguoi_10,
            )
            if m
        ]
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
        await interaction.followup.send(
            f"Đã đặt **{len(members)} Shadow Council**:\n{lines}\n\n"
            f"Gym Leader: <@{OWNER_ID}>",
            ephemeral=True,
        )

    @app_commands.command(
        name="dat_stardust",
        description="[OWNER] Xem/sửa yêu cầu chuyển stardust khi mở phòng huy hiệu.",
    )
    @app_commands.describe(
        so_tien="Số stardust yêu cầu (bỏ trống để xem cấu hình hiện tại)",
        nguoi_nhan="Người nhận stardust (mặc định là owner @hganh9128)",
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
            target = _stardust_target(target)
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
        target = _stardust_target(target)
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
        for holder_id in _council_ids(holders):
            member = await _fetch_member(guild, holder_id) if guild else None
            name = member.mention if member else f"<@{holder_id}>"
            lines.append(f"• {name}")

        amount, target, note = await asyncio.to_thread(
            _get_stardust_sync, interaction.guild_id
        )
        target = _stardust_target(target)
        stardust = _stardust_text(amount, target, note)

        embed = discord.Embed(
            title="🏅 THÁCH ĐẤU LẤY HUY HIỆU",
            color=COLOR,
            description=(
                "Thách đấu lần lượt **các Shadow Council**:\n"
                + "\n".join(lines)
                + f"\n\n{stardust}\n\n"
                + "**Cách làm:** bấm nút bên dưới hoặc dùng `/battle_bage`.\n"
                "Bot mở phòng riêng cho bạn với council — tự vào game đánh, "
                "thắng council bấm **Đã thách đấu council xong**.\n"
                "Council/Gym Leader xác nhận xong bạn mới được đánh Gym Leader, "
                "và **thắng Gym Leader mới có huy hiệu**.\n"
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
        awarded_at = await asyncio.to_thread(
            _badge_sync, interaction.guild_id, target.id
        )

        embed = discord.Embed(
            title=f"🏅 HUY HIỆU · {target.display_name}",
            color=COLOR,
        )
        if awarded_at is None:
            embed.description = "Chưa lấy huy hiệu mùa này."
        else:
            stamp = int(awarded_at.timestamp())
            embed.description = (
                "Đã lấy huy hiệu mùa này\n\n"
                f"<t:{stamp}:F> (<t:{stamp}:R>)"
            )

        embed.set_footer(text="Dark Gym · Shadow Council")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="trao_huy_hieu",
        description="[OWNER] Trao huy hiệu cho member vừa thắng Gym Leader.",
    )
    @app_commands.describe(member="Người vừa thắng Gym Leader")
    async def trao_huy_hieu(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Chỉ người quản lý Gym mới dùng được lệnh này.", ephemeral=True
            )
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Chỉ dùng được trong server.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        if not await asyncio.to_thread(_ensure_tables):
            return await interaction.followup.send(
                "Hệ thống huy hiệu đang kết nối lại database, thử lại sau ít phút.",
                ephemeral=True,
            )

        status = await asyncio.to_thread(
            _award_badge_sync, interaction.guild_id, member.id
        )
        if status == "holder":
            return await interaction.followup.send(
                f"{member.mention} là Shadow Council / Gym Leader, không trao huy hiệu.",
                ephemeral=True,
            )
        if status == "already":
            return await interaction.followup.send(
                f"{member.mention} đã có huy hiệu mùa này rồi.", ephemeral=True
            )

        channel = await get_channel(self.bot, GYM_CHANNEL_ID)
        if channel is not None:
            try:
                await channel.send(
                    f"🏅 {member.mention} đã thắng Gym Leader và nhận huy hiệu mùa này!",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            f"Đã trao huy hiệu mùa này cho {member.mention}.", ephemeral=True
        )

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
