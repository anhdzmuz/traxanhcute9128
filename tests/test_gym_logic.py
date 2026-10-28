import os
import random
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gym_logic as gl  # noqa: E402

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=gl.TZ)
OWNER, CHAL, OTHER = 111, 222, 333


def fresh():
    return gl.merge_defaults(None)


def with_owner(tier="1", user=OWNER):
    data = fresh()
    data["towers"][tier]["user_id"] = user
    return data


def tomorrow(hour, minute=0):
    day = (NOW + timedelta(days=1)).date()
    return gl.parse_hhmm(f"{hour:02d}:{minute:02d}", day)


def open_registration(data, tier="1", start_h=20, end_h=22):
    status, payload = gl.submit_schedule(data, tier, data["towers"][tier]["user_id"],
                                         tomorrow(start_h), tomorrow(end_h), NOW, channel_id=1)
    assert status == "ok", status
    return payload


# ---------------------------------------------------------------- Stardust ---

def test_stardust_first_tick_only_sets_marker():
    data = with_owner()
    assert gl.apply_stardust(data, NOW) == 0
    assert data["wallets"] == {}
    assert data["stardust_last_tick"] == NOW.isoformat()


def test_stardust_restart_does_not_pay():
    data = with_owner()
    gl.apply_stardust(data, NOW)
    for minutes in (1, 30, 179):  # nhiều lần "khởi động lại" trong vòng 3 giờ
        assert gl.apply_stardust(data, NOW + timedelta(minutes=minutes)) == 0
    assert data["wallets"] == {}


def test_stardust_pays_per_elapsed_period_and_keeps_remainder():
    data = with_owner()
    gl.apply_stardust(data, NOW)
    later = NOW + timedelta(hours=7)  # 2 kỳ đầy đủ + 1 giờ dư
    assert gl.apply_stardust(data, later) == 1
    assert data["wallets"][str(OWNER)] == 20
    assert gl.parse_dt(data["stardust_last_tick"]) == NOW + timedelta(hours=6)
    assert gl.apply_stardust(data, later) == 0  # gọi lại không trả thêm
    assert data["wallets"][str(OWNER)] == 20


def test_stardust_skips_empty_towers_and_pays_each_tower():
    data = with_owner("1")
    data["towers"]["2"]["user_id"] = OWNER
    gl.apply_stardust(data, NOW)
    assert gl.apply_stardust(data, NOW + timedelta(hours=3)) == 2
    assert data["wallets"][str(OWNER)] == 20


def test_stardust_recovers_from_bad_marker():
    data = with_owner()
    data["stardust_last_tick"] = "garbage"
    assert gl.apply_stardust(data, NOW) == 0
    assert data["stardust_last_tick"] == NOW.isoformat()


# ------------------------------------------------------------------ Lịch -----

def test_validate_window():
    assert gl.validate_window(tomorrow(20), tomorrow(22), NOW) is None
    assert gl.validate_window(NOW - timedelta(hours=1), tomorrow(22), NOW) == "past"
    assert gl.validate_window(tomorrow(22), tomorrow(20), NOW) == "order"
    assert gl.validate_window(tomorrow(6), tomorrow(9), NOW) == "range"
    assert gl.validate_window(tomorrow(20), tomorrow(23, 30), NOW) == "range"
    assert gl.validate_window(tomorrow(20), tomorrow(23, 0), NOW) is None


def test_submit_schedule_requires_current_owner():
    data = with_owner()
    assert gl.submit_schedule(data, "1", OTHER, tomorrow(20), tomorrow(22), NOW)[0] == "not_owner"


def test_submit_schedule_blocked_by_active_match():
    data = with_owner()
    data["active_matches"]["1"] = {"match_id": 1}
    assert gl.submit_schedule(data, "1", OWNER, tomorrow(20), tomorrow(22), NOW)[0] == "match"


def test_owner_can_fix_own_schedule_before_anyone_registers():
    data = with_owner()
    open_registration(data, start_h=20, end_h=22)
    status, payload = gl.submit_schedule(data, "1", OWNER, tomorrow(19), tomorrow(21), NOW, channel_id=1)
    assert status == "ok"  # trước đây bị báo "trùng với chính tầng này"
    assert gl.parse_dt(data["active_registrations"]["1"]["start_at"]) == tomorrow(19)


def test_cannot_change_schedule_once_people_registered():
    data = with_owner()
    open_registration(data)
    gl.toggle_registration(data, "1", CHAL, NOW)
    assert gl.submit_schedule(data, "1", OWNER, tomorrow(19), tomorrow(21), NOW)[0] == "has_challengers"


def test_overlap_with_other_tier_is_rejected():
    data = with_owner("1")
    data["towers"]["2"]["user_id"] = OTHER
    open_registration(data, "1", 20, 22)
    status, other = gl.submit_schedule(data, "2", OTHER, tomorrow(21), tomorrow(23), NOW)
    assert (status, other) == ("overlap", "1")
    assert gl.submit_schedule(data, "2", OTHER, tomorrow(22), tomorrow(23), NOW)[0] == "ok"


def test_submit_clears_pending_request():
    data = with_owner()
    data["pending_challenges"]["1"] = {"sent": True}
    open_registration(data)
    assert "1" not in data["pending_challenges"]


# ------------------------------------------------------------ Đăng ký + CD ---

def test_toggle_join_and_leave():
    data = with_owner()
    open_registration(data)
    assert gl.toggle_registration(data, "1", CHAL, NOW)[0] == "joined"
    assert data["active_registrations"]["1"]["challengers"] == [CHAL]
    status, payload = gl.toggle_registration(data, "1", CHAL, NOW)
    assert status == "left" and payload["count"] == 0


def test_toggle_rejects_owner_closed_and_locked():
    data = with_owner()
    assert gl.toggle_registration(data, "1", CHAL, NOW)[0] == "closed"
    open_registration(data)
    assert gl.toggle_registration(data, "1", OWNER, NOW)[0] == "owner"
    assert gl.toggle_registration(data, "1", CHAL, tomorrow(20))[0] == "locked"


def test_revenge_cooldown_is_enforced_per_tower():
    data = with_owner()
    data["active_matches"]["1"] = {"match_id": 1, "owner_id": OWNER, "challenger_id": CHAL}
    assert gl.record_result(data, "1", "thua", NOW)[0] == "ok"  # Quản Tháp thắng -> challenger bị cooldown

    open_registration(data)
    status, remaining = gl.toggle_registration(data, "1", CHAL, NOW)
    assert status == "cooldown"
    assert timedelta(hours=47) < remaining <= timedelta(hours=48)
    assert data["active_registrations"]["1"]["challengers"] == []

    # Tầng khác không bị ảnh hưởng, người khác không bị ảnh hưởng
    assert gl.toggle_registration(data, "1", OTHER, NOW)[0] == "joined"

    # Hết hạn cooldown thì đăng ký lại được và mục bị dọn
    after = NOW + timedelta(hours=49)
    data["active_registrations"]["1"]["start_at"] = (after + timedelta(hours=1)).isoformat()
    assert gl.toggle_registration(data, "1", CHAL, after)[0] == "joined"
    assert f"{CHAL}_1" not in data["cooldowns"]


def test_challenger_win_sets_no_cooldown():
    data = with_owner()
    data["active_matches"]["1"] = {"match_id": 1, "owner_id": OWNER, "challenger_id": CHAL}
    gl.record_result(data, "1", "thang", NOW)
    assert data["cooldowns"] == {}


# ------------------------------------------------------------- Bốc thăm ------

def test_resolve_not_due_returns_none():
    data = with_owner()
    open_registration(data)
    assert gl.resolve_session(data, "1", NOW) is None
    assert "1" in data["active_registrations"]


def test_resolve_picks_one_creates_match_and_closes_session():
    data = with_owner()
    open_registration(data)
    for uid in (CHAL, OTHER):
        gl.toggle_registration(data, "1", uid, NOW)
    data["active_registrations"]["1"]["challengers"].append(CHAL)  # bản ghi trùng phải được loại
    status, info = gl.resolve_session(data, "1", tomorrow(20), rng=random.Random(1))
    assert status == "match"
    assert info["selected"] in (CHAL, OTHER) and info["count"] == 2 and info["owner_id"] == OWNER
    assert "1" not in data["active_registrations"]
    assert data["active_matches"]["1"]["match_id"] == 1
    # Gọi lần hai (vòng lặp chạy lại) không tạo trận thứ hai
    assert gl.resolve_session(data, "1", tomorrow(20)) is None
    assert len(data["active_matches"]) == 1


def test_resolve_empty_and_cancelled_and_invalid():
    data = with_owner()
    open_registration(data)
    assert gl.resolve_session(data, "1", tomorrow(20))[0] == "empty"
    assert data["active_registrations"] == {}

    open_registration(data)
    gl.toggle_registration(data, "1", CHAL, NOW)
    data["towers"]["1"]["user_id"] = None
    assert gl.resolve_session(data, "1", tomorrow(20))[0] == "cancelled"

    data["active_registrations"]["1"] = {"start_at": "bad"}
    assert gl.resolve_session(data, "1", tomorrow(20)) == ("invalid", None)
    assert data["active_registrations"] == {}


def test_next_match_id_counts_history_and_active():
    data = fresh()
    data["match_history"] = [{"match_id": 4}, {"match_id": 2}]
    data["active_matches"]["2"] = {"match_id": 7}
    assert gl.next_match_id(data) == 8


# ---------------------------------------------------- Owner / yêu cầu / hủy ---

def test_request_challenge_rules():
    data = fresh()
    assert gl.request_challenge(data, "1", 1, NOW)[0] == "no_owner"
    data["towers"]["1"]["user_id"] = OWNER
    data["towers"]["1"]["protected_until"] = (NOW + timedelta(hours=5)).isoformat()
    status, left = gl.request_challenge(data, "1", 1, NOW)
    assert status == "protected" and left == timedelta(hours=5)
    data["towers"]["1"]["protected_until"] = (NOW - timedelta(hours=1)).isoformat()
    assert gl.request_challenge(data, "1", 1, NOW)[0] == "ok"
    assert "1" in data["pending_challenges"]
    gl.cancel_request(data, "1")
    assert data["pending_challenges"] == {}
    data["active_matches"]["1"] = {}
    assert gl.request_challenge(data, "1", 1, NOW)[0] == "busy"


def test_cancel_challenge_returns_message_to_delete():
    data = with_owner()
    open_registration(data)
    gl.mark_announced(data, "1", data["active_registrations"]["1"]["start_at"], NOW, 999)
    removed, message_id, channel_id = gl.cancel_challenge(data, "1")
    assert (removed, message_id, channel_id) == (True, 999, 1)
    assert gl.cancel_challenge(data, "1") == (False, None, None)


def test_mark_announced_ignores_replaced_schedule():
    data = with_owner()
    open_registration(data, start_h=20, end_h=22)
    old_start = data["active_registrations"]["1"]["start_at"]
    open_registration(data, start_h=19, end_h=21)
    assert gl.mark_announced(data, "1", old_start, NOW, 5) is False
    assert data["active_registrations"]["1"]["announcement_message_id"] is None


def test_promote_blocked_while_match_active_otherwise_resets():
    data = with_owner()
    open_registration(data)
    data["towers"]["1"]["defense_streak"] = 4
    data["active_matches"]["1"] = {"match_id": 1}
    assert gl.promote(data, "1", OTHER, NOW)[0] == "match"
    assert data["towers"]["1"]["user_id"] == OWNER  # không đổi gì

    del data["active_matches"]["1"]
    status, info = gl.promote(data, "1", OTHER, NOW)
    assert status == "ok" and info["old_owner"] == OWNER
    tower = data["towers"]["1"]
    assert tower["user_id"] == OTHER and tower["defense_streak"] == 0
    assert gl.parse_dt(tower["protected_until"]) == NOW + timedelta(hours=12)
    assert data["active_registrations"] == {}


def test_clear_protection():
    data = with_owner()
    assert gl.clear_protection(data, "1")[0] == "none"
    data["towers"]["1"]["protected_until"] = NOW.isoformat()
    assert gl.clear_protection(data, "1")[0] == "ok"
    assert data["towers"]["1"]["protected_until"] is None


# ------------------------------------------------------------ Kết quả trận ---

def make_match(data, tier="1", owner=OWNER, challenger=CHAL, match_id=1):
    data["active_matches"][tier] = {"match_id": match_id, "owner_id": owner, "challenger_id": challenger}


def test_record_result_without_match():
    assert gl.record_result(with_owner(), "1", "thang", NOW) == ("no_match", None)


def test_challenger_win_takes_tower():
    data = with_owner()
    make_match(data)
    status, info = gl.record_result(data, "1", "thang", NOW)
    assert status == "ok" and info["match_id"] == 1
    tower = data["towers"]["1"]
    assert tower["user_id"] == CHAL and tower["defense_streak"] == 0
    assert gl.parse_dt(tower["protected_until"]) == NOW + timedelta(hours=12)
    assert data["players"][str(CHAL)]["captures"] == 1
    assert data["players"][str(OWNER)]["losses"] == 1
    assert "⚔️ First Blood" in info["achievements"] and "👑 Conqueror" in info["achievements"]
    assert data["active_matches"] == {}
    assert data["match_history"][-1]["result"] == "thang"


def test_owner_defense_rewards_stardust_and_streak():
    data = with_owner()
    make_match(data)
    gl.record_result(data, "1", "thua", NOW)
    assert data["wallets"][str(OWNER)] == 15
    assert data["towers"]["1"]["user_id"] == OWNER
    assert data["towers"]["1"]["defense_streak"] == 1
    assert data["players"][str(OWNER)]["defenses"] == 1


def test_record_result_twice_is_idempotent():
    data = with_owner()
    make_match(data)
    assert gl.record_result(data, "1", "thua", NOW)[0] == "ok"
    assert gl.record_result(data, "1", "thua", NOW)[0] == "no_match"
    assert data["wallets"][str(OWNER)] == 15  # không bị cộng đôi


def test_history_is_capped_at_100():
    data = with_owner()
    for i in range(1, 106):
        make_match(data, match_id=i)
        gl.record_result(data, "1", "thua", NOW)
    assert len(data["match_history"]) == 100
    assert data["match_history"][-1]["match_id"] == 105
    assert gl.next_match_id(data) == 106


# ----------------------------------------------------------------- Schema ----

def test_merge_defaults_keeps_data_and_adds_new_fields():
    old = {"towers": {"1": {"user_id": 5}}, "wallets": {"5": 40}, "players": "oops"}
    merged = gl.merge_defaults(old)
    assert merged["towers"]["1"]["user_id"] == 5
    assert merged["towers"]["1"]["title"] == "Antumbra Overlord"
    assert merged["wallets"] == {"5": 40}
    assert merged["players"] == {}
    assert "stardust_last_tick" in merged and set(merged["towers"]) == {"1", "2", "3"}


def test_announcement_text_always_shows_time_window():
    data = with_owner()
    session = open_registration(data, start_h=19, end_h=21)["session"]
    new = gl.announcement_text("1", session)
    again = gl.announcement_text("1", session, reminder=True)
    for text in (new, again):
        assert "19:00 – 21:00" in text and "29/10/2026" in text and "Tầng 1" in text
    assert new.startswith("Lịch thách đấu mới") and again.startswith("⏰ Nhắc lại")


def test_format_remaining():
    assert gl.format_remaining(timedelta(days=2, hours=3)) == "2 ngày 3 giờ"
    assert gl.format_remaining(timedelta(hours=1, minutes=5)) == "1 giờ 5 phút"
    assert gl.format_remaining(timedelta(minutes=7)) == "7 phút"
    assert gl.format_remaining(timedelta(seconds=-5)) == "0 phút"
