"""Test luật giải đấu tuần trong `match_logic.py`.

Mốc thời gian dùng xuyên suốt: tuần 2026-W45
    Thứ 2 02/11/2026 00:00  -> mở đăng ký
    CN    08/11/2026 00:00  -> bốc thăm
    CN    08/11/2026 06:00  -> thông báo lại
    CN    08/11/2026 18:00  -> ping
    CN    08/11/2026 23:59  -> hạn chót
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import match_logic as ml  # noqa: E402

LEADER = 1000
PLAYER_A = 2001
PLAYER_B = 2002
PLAYER_C = 2003


def at(day, hour=0, minute=0, second=0):
    """Giờ Việt Nam trong tuần 2026-W45 (02/11 -> 08/11)."""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=ml.TZ)


MONDAY = at(2, 0, 1)
WEDNESDAY = at(4, 12)
SUNDAY_DRAW = at(8, 0, 5)
SUNDAY_NOTICE = at(8, 6, 0)
SUNDAY_PING = at(8, 18, 0)
SUNDAY_END = at(8, 23, 59, 59)
# 23:59:59.5 — đã qua hạn chót nhưng vẫn còn trong tuần W45.
SUNDAY_OVERDUE = SUNDAY_END + timedelta(milliseconds=500)
NEXT_MONDAY = at(9, 0, 1)


class FirstChoiceRng:
    """`random` giả: luôn lấy k phần tử đầu tiên."""

    def sample(self, population, k):
        return list(population)[:k]


def fresh_state(leader_id=LEADER):
    state = ml.merge_defaults({})
    if leader_id is not None:
        start = ml.week_start(MONDAY)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        state["leader"] = {
            "user_id": leader_id,
            "term_start": start.isoformat(),
            "term_end": end.isoformat(),
        }
    return state


def open_week(registrations=(), leader_id=LEADER):
    state = fresh_state(leader_id)
    actions = ml.ensure_week(state, MONDAY)
    assert actions == [("open", {"week_id": ml.week_id(MONDAY)})]
    state["week"]["registrations"] = list(registrations)
    return state


# --------------------------------------------------------------------------- #
# Mở tuần & đăng ký
# --------------------------------------------------------------------------- #

def test_monday_opens_the_week_once():
    state = fresh_state()
    assert ml.ensure_week(state, MONDAY) == [("open", {"week_id": ml.week_id(MONDAY)})]
    assert state["week"]["phase"] == "open"
    assert state["week"]["week_id"] == ml.week_id(MONDAY)
    assert state["week"]["registrations"] == []
    assert ml.ensure_week(state, MONDAY) == [], "gọi lại không được mở tuần lần nữa"


def test_midweek_is_quiet():
    state = open_week([PLAYER_A])
    assert ml.ensure_week(state, WEDNESDAY) == []
    assert state["week"]["phase"] == "open"


def test_registration_toggles_and_blocks_leader():
    state = open_week()
    assert ml.toggle_registration(state, PLAYER_A, MONDAY)[0] == "joined"
    assert ml.toggle_registration(state, PLAYER_B, MONDAY) == ("joined", {"count": 2})
    assert ml.toggle_registration(state, PLAYER_A, MONDAY) == ("left", {"count": 1})
    assert ml.toggle_registration(state, LEADER, MONDAY)[0] == "leader"

    state["week"]["phase"] = "qualifier"
    assert ml.toggle_registration(state, PLAYER_C, MONDAY)[0] == "closed"


# --------------------------------------------------------------------------- #
# Bốc thăm Chủ Nhật 00:00
# --------------------------------------------------------------------------- #

def test_draw_two_from_many_bo1_and_priority_bump():
    state = open_week([PLAYER_A, PLAYER_B, PLAYER_C])
    actions = ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())

    assert actions[0][0] == "draw_pair"
    assert state["week"]["phase"] == "qualifier"
    assert state["week"]["qualifier"]["best_of"] == 1
    picked = state["week"]["qualifier"]["players"]
    assert picked == [PLAYER_A, PLAYER_B]

    # Người không được chọn được cộng ưu tiên cho tuần sau.
    assert state["priority"][str(PLAYER_C)] == 1
    assert state["priority"][str(PLAYER_A)] == 0


def test_draw_prefers_players_with_priority():
    state = open_week([PLAYER_A, PLAYER_B, PLAYER_C])
    state["priority"] = {str(PLAYER_C): 2, str(PLAYER_A): 1, str(PLAYER_B): 0}

    ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())

    picked = state["week"]["qualifier"]["players"]
    assert picked[0] == PLAYER_C, "người có ưu tiên cao nhất phải chắc suất"


def test_single_registration_challenges_leader_bo3():
    state = open_week([PLAYER_A])
    actions = ml.ensure_week(state, SUNDAY_DRAW)

    assert actions[0][0] == "draw_single"
    assert state["week"]["phase"] == "title"
    assert state["week"]["title"] == {
        "challenger_id": PLAYER_A,
        "leader_id": LEADER,
        "best_of": 3,
    }
    assert state["priority"][str(PLAYER_A)] == 0


def test_no_registration_extends_leader_term():
    state = open_week([])
    old_end = state["leader"]["term_end"]

    actions = ml.ensure_week(state, SUNDAY_DRAW)

    assert actions[0][0] == "draw_empty"
    assert state["week"]["phase"] == "done"
    start, end = ml.next_term(MONDAY)
    assert state["leader"]["user_id"] == LEADER
    assert state["leader"]["term_end"] == end.isoformat()
    assert state["leader"]["term_end"] != old_end


def test_leader_is_ignored_if_they_registered():
    state = open_week([LEADER, PLAYER_A])
    actions = ml.ensure_week(state, SUNDAY_DRAW)

    assert actions[0][0] == "draw_single"
    assert state["week"]["title"]["challenger_id"] == PLAYER_A


def test_first_champion_is_crowned_without_leader():
    state = open_week([PLAYER_A, PLAYER_B], leader_id=None)
    ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())

    status, payload = ml.record_qualifier(state, PLAYER_A, SUNDAY_DRAW)

    assert status == "ok"
    assert payload["crowned"] == PLAYER_A
    assert state["week"]["phase"] == "done"
    assert state["leader"]["user_id"] == PLAYER_A
    assert state["players"][str(PLAYER_A)]["titles"] == 1


# --------------------------------------------------------------------------- #
# Chốt kết quả
# --------------------------------------------------------------------------- #

def test_record_qualifier_winner_meets_leader():
    state = open_week([PLAYER_A, PLAYER_B])
    ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())

    status, payload = ml.record_qualifier(state, PLAYER_A, SUNDAY_DRAW)

    assert status == "ok"
    assert payload["challenger_id"] == PLAYER_A
    assert payload["leader_id"] == LEADER
    assert state["week"]["phase"] == "title"
    assert state["players"][str(PLAYER_A)]["wins"] == 1
    assert state["players"][str(PLAYER_A)]["streak"] == 1
    assert state["players"][str(PLAYER_B)]["losses"] == 1
    assert state["history"][-1]["type"] == "qualifier"
    assert state["history"][-1]["winner_id"] == PLAYER_A


def test_record_qualifier_rejects_outsider_and_wrong_phase():
    state = open_week([PLAYER_A, PLAYER_B])
    ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())

    assert ml.record_qualifier(state, PLAYER_C, SUNDAY_DRAW)[0] == "not_in_match"

    state["week"]["phase"] = "open"
    assert ml.record_qualifier(state, PLAYER_A, SUNDAY_DRAW)[0] == "phase"


def _to_title(state):
    ml.ensure_week(state, SUNDAY_DRAW)
    ml.record_qualifier(state, PLAYER_A, SUNDAY_DRAW)


def test_record_title_challenger_becomes_leader():
    state = open_week([PLAYER_A])
    _to_title(state)

    status, payload = ml.record_title(state, PLAYER_A, SUNDAY_END)

    assert status == "ok"
    assert payload["leader_changed"] is True
    assert state["leader"]["user_id"] == PLAYER_A
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_start"] == start.isoformat()
    assert state["leader"]["term_end"] == end.isoformat()
    assert state["players"][str(PLAYER_A)]["titles"] == 1
    assert state["week"]["phase"] == "done"
    assert state["history"][-1]["type"] == "title"


def test_record_title_leader_defends_and_extends():
    state = open_week([PLAYER_A])
    _to_title(state)

    status, payload = ml.record_title(state, LEADER, SUNDAY_END)

    assert status == "ok"
    assert payload["leader_changed"] is False
    assert state["leader"]["user_id"] == LEADER
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_end"] == end.isoformat()
    assert state["players"][str(LEADER)]["wins"] == 1
    assert state["players"][str(PLAYER_A)]["losses"] == 1
    assert state["players"][str(LEADER)]["titles"] == 0


def test_record_title_rejects_outsider():
    state = open_week([PLAYER_A])
    _to_title(state)
    assert ml.record_title(state, PLAYER_B, SUNDAY_END)[0] == "not_in_match"


# --------------------------------------------------------------------------- #
# Nhắc & hết hạn
# --------------------------------------------------------------------------- #

def test_notice_and_ping_fire_once():
    state = open_week([PLAYER_A])
    _to_title(state)

    assert ml.ensure_week(state, SUNDAY_NOTICE) == [
        ("notice", {"week_id": ml.week_id(SUNDAY_NOTICE)})
    ]
    assert ml.ensure_week(state, SUNDAY_NOTICE + timedelta(minutes=2)) == []

    actions = ml.ensure_week(state, SUNDAY_PING)
    assert [kind for kind, _ in actions] == ["ping"]
    assert ml.ensure_week(state, SUNDAY_PING + timedelta(minutes=2)) == []


def test_deadline_extends_leader():
    state = open_week([PLAYER_A])
    _to_title(state)
    state["week"]["flags"] = {"notice": True, "ping": True}

    actions = ml.ensure_week(state, SUNDAY_OVERDUE)

    assert [kind for kind, _ in actions] == ["timeout"]
    assert state["week"]["phase"] == "done"
    assert state["leader"]["user_id"] == LEADER
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_end"] == end.isoformat()


def test_rollover_after_unfinished_week_extends_and_reopens():
    state = open_week([PLAYER_A])
    _to_title(state)

    actions = ml.ensure_week(state, NEXT_MONDAY)

    assert [kind for kind, _ in actions] == ["timeout", "open"]
    assert state["week"]["phase"] == "open"
    assert state["week"]["week_id"] == ml.week_id(NEXT_MONDAY)
    assert state["week"]["registrations"] == []
    assert state["leader"]["user_id"] == LEADER
    start, end = ml.next_term(NEXT_MONDAY)
    assert state["leader"]["term_end"] == end.isoformat()


def test_bot_down_all_sunday_reports_missed():
    state = open_week([PLAYER_A])

    actions = ml.ensure_week(state, NEXT_MONDAY)

    assert [kind for kind, _ in actions] == ["missed", "open"]
    assert state["week"]["phase"] == "open"
    assert state["leader"]["user_id"] == LEADER


def test_bot_back_late_sunday_does_not_draw():
    state = open_week([PLAYER_A, PLAYER_B])

    actions = ml.ensure_week(state, SUNDAY_OVERDUE)

    assert [kind for kind, _ in actions] == ["missed"]
    assert state["week"]["phase"] == "done"


# --------------------------------------------------------------------------- #
# Thời gian & schema
# --------------------------------------------------------------------------- #

def test_schedule_helpers():
    assert ml.week_start(MONDAY) == at(2)
    assert ml.draw_at(WEDNESDAY) == at(8)
    assert ml.notice_at(WEDNESDAY) == at(8, 6)
    assert ml.ping_at(WEDNESDAY) == at(8, 18)
    assert ml.deadline_at(WEDNESDAY) == at(8, 23, 59, 59)
    assert ml.week_id(MONDAY) == "2026-W45"


def test_next_term_is_the_following_week():
    start, end = ml.next_term(SUNDAY_DRAW)
    assert start == at(9), "nhiệm kỳ bắt đầu thứ 2 tuần sau"
    assert end == at(15, 23, 59, 59)
    assert end - start == timedelta(days=6, hours=23, minutes=59, seconds=59)


def test_merge_defaults_migrates_legacy_data():
    legacy = {
        "players": {
            "2001": {"wins": 4, "losses": 1, "captures": 2, "defenses": 3},
            "2002": {"wins": "3", "losses": None},
        },
        "match_history": [
            {
                "match_id": 7,
                "tower": 2,
                "challenger_id": PLAYER_A,
                "owner_id": LEADER,
                "result": "thang",
                "ended_at": "2026-10-01T21:00:00+07:00",
            },
            {
                "match_id": 8,
                "tower": 1,
                "challenger_id": PLAYER_B,
                "owner_id": LEADER,
                "result": "thua",
                "ended_at": "2026-10-02T21:00:00+07:00",
            },
            "rác",
        ],
        "towers": {"1": {}},
        "wallets": {"2001": 999},
    }

    state = ml.merge_defaults(legacy)

    assert state["players"][str(PLAYER_A)]["wins"] == 4
    assert "captures" not in state["players"][str(PLAYER_A)]
    assert state["players"][str(PLAYER_B)]["wins"] == 3
    assert state["players"][str(PLAYER_B)]["losses"] == 0
    assert "towers" not in state and "wallets" not in state

    assert len(state["history"]) == 2
    first, second = state["history"]
    assert first["type"] == "legacy"
    assert first["winner_id"] == PLAYER_A
    assert second["winner_id"] == LEADER, "thua nghĩa là chủ tháp thắng"
    assert state["week"]["phase"] == "idle"
    assert state["next_match_id"] == 1


def test_merge_defaults_keeps_current_state():
    state = open_week([PLAYER_A])
    ml.ensure_week(state, SUNDAY_DRAW)

    again = ml.merge_defaults(state)

    assert again["week"]["phase"] == "title"
    assert again["week"]["title"]["challenger_id"] == PLAYER_A
    assert again["week"]["week_id"] == state["week"]["week_id"]


def test_merge_defaults_repairs_broken_week():
    state = ml.merge_defaults(
        {
            "week": {
                "week_id": "2026-W45",
                "phase": "không tồn tại",
                "registrations": "hỏng",
                "picked": None,
                "flags": [],
            }
        }
    )

    assert state["week"]["phase"] == "idle"
    assert state["week"]["registrations"] == []
    assert state["week"]["picked"] == []
    assert state["week"]["flags"] == {}


def test_leader_term_text():
    state = fresh_state()
    text = ml.leader_term_text(state)
    assert f"<@{LEADER}>" in text and "<t:" in text
    assert ml.leader_term_text(ml.merge_defaults({})) == "Chưa có Gym Leader"


def test_week_summary_shows_current_phase():
    state = open_week([PLAYER_A, PLAYER_B])
    summary = ml.week_summary(state, WEDNESDAY)
    assert "Đang mở đăng ký" in summary
    assert "**Đã đăng ký:** 2" in summary

    ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())
    summary = ml.week_summary(state, SUNDAY_DRAW)
    assert "Đang đánh BO1" in summary
    assert f"<@{PLAYER_A}>" in summary
