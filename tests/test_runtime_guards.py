"""Test các chốt chặn tải: cache đọc dữ liệu và giới hạn bấm nút."""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.Challenge as challenge  # noqa: E402
import cogs.core as core  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state():
    core._invalidate_cache()
    challenge._last_click.clear()
    challenge._pending_refresh.clear()
    yield
    core._invalidate_cache()
    challenge._last_click.clear()
    challenge._pending_refresh.clear()


def test_read_data_is_cached_between_calls(monkeypatch):
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return {"towers": {}, "wallets": {}, "players": {}}

    monkeypatch.setattr(core, "load_data", fake_load)

    async def scenario():
        first = await core.read_data()
        second = await core.read_data()
        third = await core.read_data(fresh=True)
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert calls["n"] == 2, "lần đọc thứ hai phải lấy từ cache"
    assert first == second == third
    assert first is not second, "cache phải trả bản sao, không lộ dict dùng chung"


def test_write_invalidates_cache(monkeypatch):
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return {"towers": {}, "wallets": {}, "players": {}}

    monkeypatch.setattr(core, "load_data", fake_load)

    async def scenario():
        await core.read_data()
        core._invalidate_cache()  # mô phỏng update_data vừa ghi
        await core.read_data()

    asyncio.run(scenario())
    assert calls["n"] == 2


def test_click_cooldown_blocks_repeat_clicks():
    user = 4242
    assert challenge._click_allowed(user) is True
    assert challenge._click_allowed(user) is False
    assert challenge._click_allowed(user + 1) is True, "người khác không bị ảnh hưởng"

    challenge._last_click[user] = time.monotonic() - challenge.CLICK_COOLDOWN - 1
    assert challenge._click_allowed(user) is True


def test_click_cooldown_prunes_old_entries():
    moment = time.monotonic()
    for uid in range(5100):
        challenge._last_click[uid] = moment - 120  # đều đã cũ
    assert challenge._click_allowed(999999) is True
    assert len(challenge._last_click) < 5100
