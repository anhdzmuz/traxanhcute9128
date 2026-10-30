import asyncio
import copy
import json
import logging
import os
import time

import discord
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from discord import app_commands

import match_logic as ml

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


_pool = None

STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "5000"))


def _configure_connection(conn):
    """Đặt statement_timeout SAU khi kết nối, không qua startup packet.

    Neon chạy PgBouncer ở cổng pooler và từ chối tham số ``options`` trong
    startup packet ("unsupported startup parameter in options: statement_timeout"),
    làm mọi lần kết nối thất bại. ``SET`` lúc runtime có tác dụng tương đương
    nhưng đi qua giao thức thường nên pooler chấp nhận.
    """
    if STATEMENT_TIMEOUT_MS > 0:
        conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        # SET nằm trong transaction; phải commit nếu không pool sẽ rollback
        # khi trả kết nối về và cấu hình bị mất.
        conn.commit()


def get_pool():
    """Pool kết nối dùng chung.

    Trước đây mỗi lần bấm nút / chạy lệnh đều mở một kết nối TCP+TLS mới tới
    PostgreSQL. Trên server đông người, chính chi phí bắt tay đó làm bot chậm và
    dễ vượt giới hạn connection của nhà cung cấp DB. Pool giữ sẵn kết nối và
    tái sử dụng chúng.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _database_url(),
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", "8")),
            timeout=15,
            kwargs={
                "connect_timeout": 10,
                # PostgreSQL hosted externally should use TLS.
                "sslmode": "require",
            },
            configure=_configure_connection,
            open=True,
            name="dark-gym",
        )
    return _pool


def _connect():
    return get_pool().connection()


def initialize_storage(attempts=5, delay=3.0):
    """Create the PostgreSQL table and initialize a clean state only if empty.

    The old local gym_data.json is intentionally ignored. Existing database
    data is never reset during restarts or deployments.

    Có thử lại vài lần: Neon free tier ngủ đông sau một thời gian không dùng, lần
    kết nối đầu tiên có thể chậm hoặc lỗi tạm thời. Không có retry thì bot chết
    ngay lúc khởi động và người dùng chỉ thấy "ứng dụng không phản hồi".
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
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
                    (DATA_KEY, Jsonb(copy.deepcopy(ml.DEFAULT_STATE))),
                )
            print("✅ PostgreSQL storage ready; clean state is used when the database is empty.")
            return
        except Exception as exc:  # psycopg lỗi mạng/DB đều cần thử lại
            last_error = exc
            print(f"⚠️ Không kết nối được PostgreSQL (lần {attempt}/{attempts}): {exc}")
            if attempt < attempts:
                time.sleep(delay)
    raise RuntimeError("Không khởi tạo được PostgreSQL storage sau nhiều lần thử.") from last_error


def _decode_row(row):
    """Giải mã payload; báo lỗi rõ ràng thay vì âm thầm làm mất quyền sở hữu."""
    if row is None:
        raise RuntimeError("Không tìm thấy dữ liệu Dark Gym trong PostgreSQL; từ chối tự tạo lại để tránh mất dữ liệu.")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise RuntimeError("Dữ liệu Dark Gym trong PostgreSQL không hợp lệ; từ chối ghi đè.")
    return ml.merge_defaults(data)


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


_CACHE_TTL_DEFAULT = "2.5"
CACHE_TTL = float(os.environ.get("DATA_CACHE_TTL", _CACHE_TTL_DEFAULT))
_cache_at = 0.0
_cache_data = None


def _invalidate_cache():
    global _cache_at, _cache_data
    _cache_at, _cache_data = 0.0, None


async def read_data(fresh: bool = False):
    """Đọc trạng thái mà không chặn event loop.

    Có cache ngắn (mặc định 2.5 giây) để nhiều người cùng xem `/thap_gym`,
    `/dark_rank`... không tạo ra hàng loạt truy vấn giống nhau. Mọi thao tác ghi
    đều xóa cache, nên dữ liệu hiển thị vẫn đúng ngay sau khi có thay đổi.
    """
    global _cache_at, _cache_data
    if not fresh and _cache_data is not None and (time.monotonic() - _cache_at) < CACHE_TTL:
        return copy.deepcopy(_cache_data)
    data = await asyncio.to_thread(load_data)
    _cache_at, _cache_data = time.monotonic(), copy.deepcopy(data)
    return data


async def update_data(fn):
    """Đọc – sửa – ghi nguyên tử.

    `fn(data)` chạy trong một transaction có khóa dòng; nó sửa trực tiếp `data`
    và trả về kết quả bất kỳ. `fn` phải đồng bộ, nhanh và KHÔNG gọi Discord:
    hãy trả về thông tin cần thiết rồi gửi tin nhắn sau khi `update_data` kết thúc.
    Nếu `fn` ném lỗi, transaction được rollback và không có gì được ghi.
    """
    result = await asyncio.to_thread(_update_sync, fn)
    _invalidate_cache()
    return result


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

# Module này chỉ cung cấp hạ tầng (pool, cache, helper) cho các cog khác.
# `load_extension` vẫn cần một hàm `setup`, nên giữ lại một setup rỗng.


async def setup(bot):
    pass
