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
    assert [kind for kind, _ in actions] == ["open"]
    assert actions[0][1]["week_id"] == ml.week_id(MONDAY)
    state["week"]["registrations"] = list(registrations)
    return state


# --------------------------------------------------------------------------- #
# Mở tuần & đăng ký
# --------------------------------------------------------------------------- #

def test_monday_opens_the_week_once():
    state = fresh_state()
    actions = ml.ensure_week(state, MONDAY)
    assert [kind for kind, _ in actions] == ["open"]
    assert actions[0][1] == {"week_id": ml.week_id(MONDAY), "wl": 1}
    assert state["week"]["phase"] == "open"
    assert state["week"]["week_id"] == ml.week_id(MONDAY)
    assert state["week"]["registrations"] == []
    assert ml.ensure_week(state, MONDAY) == [], "gọi lại không được mở tuần lần nữa"


def test_wl_counter_increments_each_week():
    state = fresh_state()
    ml.ensure_week(state, MONDAY)
    assert state["week"]["wl"] == 1

    ml.ensure_week(state, NEXT_MONDAY)
    assert state["week"]["wl"] == 2

    ml.ensure_week(state, at(16, 0, 1))
    assert state["week"]["wl"] == 3


def test_panel_reposts_every_8_hours():
    state = open_week([PLAYER_A])
    # Vừa mở tuần: chưa tới hạn đăng lại.
    assert ml.ensure_week(state, MONDAY + timedelta(hours=7, minutes=59)) == []

    actions = ml.ensure_week(state, MONDAY + timedelta(hours=8, minutes=1))
    assert [kind for kind, _ in actions] == ["repost"]

    # Không đăng lại lần nữa ngay sau đó.
    assert ml.ensure_week(state, MONDAY + timedelta(hours=8, minutes=3)) == []
    actions = ml.ensure_week(state, MONDAY + timedelta(hours=16, minutes=5))
    assert [kind for kind, _ in actions] == ["repost"]


def test_registration_toggles_and_blocks_leader():
    state = open_week()
    assert ml.toggle_registration(state, PLAYER_A, MONDAY)[0] == "joined"
    assert ml.toggle_registration(state, PLAYER_B, MONDAY) == ("joined", {"count": 2})
    assert ml.toggle_registration(state, PLAYER_A, MONDAY) == ("left", {"count": 1})
    assert ml.toggle_registration(state, LEADER, MONDAY)[0] == "leader"

    state["week"]["phase"] = "qualifier"
    assert ml.toggle_registration(state, PLAYER_C, MONDAY)[0] == "closed"


def test_leave_registration_never_rejoins():
    state = open_week()
    ml.toggle_registration(state, PLAYER_A, MONDAY)

    assert ml.leave_registration(state, PLAYER_A, MONDAY) == ("left", {"count": 0})
    assert ml.leave_registration(state, PLAYER_A, MONDAY) == ("absent", {"count": 0})
    assert state["week"]["registrations"] == []

    state["week"]["phase"] = "qualifier"
    assert ml.leave_registration(state, PLAYER_A, MONDAY)[0] == "closed"


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


def test_first_champion_is_elected_without_leader():
    state = open_week([PLAYER_A, PLAYER_B], leader_id=None)
    ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())

    status, payload = ml.record_qualifier(state, PLAYER_A, SUNDAY_DRAW)

    assert status == "ok"
    assert payload["crowned"] == PLAYER_A
    assert state["week"]["phase"] == "done"
    assert state["leader"]["user_id"] is None, "chưa nhậm chức ngay"
    assert state["leader_elect"] == PLAYER_A

    # 00:00 thứ 2 mới chính thức thành Gym Leader.
    actions = ml.ensure_week(state, NEXT_MONDAY)
    assert [kind for kind, _ in actions] == ["promote", "open"]
    assert state["leader"]["user_id"] == PLAYER_A
    assert state["leader_elect"] is None
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


def test_record_title_challenger_is_elected_not_crowned_yet():
    state = open_week([PLAYER_A])
    _to_title(state)

    status, payload = ml.record_title(state, PLAYER_A, SUNDAY_END)

    assert status == "ok"
    assert payload["leader_changed"] is True
    assert state["leader"]["user_id"] == LEADER, "leader cũ vẫn tại vị tới hết Chủ Nhật"
    assert state["leader_elect"] == PLAYER_A
    assert state["players"][str(PLAYER_A)]["titles"] == 0
    assert state["week"]["phase"] == "done"
    assert state["history"][-1]["type"] == "title"


def test_title_winner_takes_office_at_monday_midnight():
    state = open_week([PLAYER_A])
    _to_title(state)
    ml.record_title(state, PLAYER_A, SUNDAY_END)

    actions = ml.ensure_week(state, NEXT_MONDAY)

    assert [kind for kind, _ in actions] == ["promote", "open"]
    assert actions[0][1]["user_id"] == PLAYER_A
    assert state["leader"]["user_id"] == PLAYER_A
    assert state["leader_elect"] is None
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_start"] == start.isoformat()
    assert state["leader"]["term_end"] == end.isoformat()
    assert state["players"][str(PLAYER_A)]["titles"] == 1


def test_record_title_leader_defends_then_term_extends_at_monday():
    state = open_week([PLAYER_A])
    _to_title(state)

    status, payload = ml.record_title(state, LEADER, SUNDAY_END)

    assert status == "ok"
    assert payload["leader_changed"] is False
    assert state["leader"]["user_id"] == LEADER
    assert state["leader_elect"] == LEADER
    assert state["players"][str(LEADER)]["wins"] == 1
    assert state["players"][str(PLAYER_A)]["losses"] == 1

    ml.ensure_week(state, NEXT_MONDAY)

    assert state["leader"]["user_id"] == LEADER
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_end"] == end.isoformat()
    assert state["players"][str(LEADER)]["titles"] == 0, "bảo vệ chức không tính danh hiệu mới"


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
    # Nhiệm kỳ tính cho tuần mới (bắt đầu ngay lúc 00:00 thứ 2), không nhảy thêm tuần.
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_start"] == start.isoformat()
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


# --------------------------------------------------------------------------- #
# Nộp – xác nhận kết quả trong chat riêng
# --------------------------------------------------------------------------- #

PROOF = {"message_id": 11, "channel_id": 22, "url": "https://discord.com/x", "filename": "a.png"}


def qualifier_state():
    state = open_week([PLAYER_A, PLAYER_B])
    ml.ensure_week(state, SUNDAY_DRAW, rng=FirstChoiceRng())
    assert state["week"]["phase"] == "qualifier"
    return state


def test_parse_score_valid_and_invalid():
    assert ml.parse_score("1-0", 1) == (1, 0)
    assert ml.parse_score(" 0 : 1 ", 1) == (0, 1)
    assert ml.parse_score("2-1", 3) == (2, 1)
    assert ml.parse_score("1-2", 3) == (1, 2)

    assert ml.parse_score("2-1", 1) is None, "BO1 không thể có tỷ số 2-1"
    assert ml.parse_score("1-0", 3) is None, "BO3 phải đủ 2 game thắng"
    assert ml.parse_score("thắng", 3) is None
    assert ml.parse_score("", 3) is None


def test_submit_derives_winner_from_submitter_view():
    state = qualifier_state()

    status, payload = ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)
    assert status == "ok"
    assert payload["winner_id"] == PLAYER_A
    assert payload["opponent_id"] == PLAYER_B
    assert state["week"]["submissions"]["qualifier"][str(PLAYER_A)]["status"] == "pending"

    # B nộp tỷ số theo góc nhìn của B: 0-1 nghĩa là B thua.
    state = qualifier_state()
    status, payload = ml.submit_result(state, PLAYER_B, "0-1", PROOF, SUNDAY_DRAW)
    assert status == "ok"
    assert payload["winner_id"] == PLAYER_A


def test_submit_rejects_outsider_bad_score_and_wrong_phase():
    state = qualifier_state()
    assert ml.submit_result(state, PLAYER_C, "1-0", PROOF, SUNDAY_DRAW)[0] == "not_in_match"
    assert ml.submit_result(state, PLAYER_A, "3-0", PROOF, SUNDAY_DRAW)[0] == "bad_score"

    state["week"]["phase"] = "open"
    assert ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)[0] == "phase"


def test_submit_twice_overwrites_until_confirmed():
    state = qualifier_state()
    ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)
    ml.submit_result(state, PLAYER_A, "0-1", PROOF, SUNDAY_DRAW)
    assert state["week"]["submissions"]["qualifier"][str(PLAYER_A)]["score"] == "0-1"

    state["week"]["submissions"]["qualifier"][str(PLAYER_A)]["status"] = "confirmed"
    assert ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)[0] == "already"


def test_confirm_qualifier_moves_to_title():
    state = qualifier_state()
    ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)

    status, payload = ml.confirm_result(state, PLAYER_A, SUNDAY_DRAW)

    assert status == "ok"
    assert payload["winner_id"] == PLAYER_A
    assert state["week"]["phase"] == "title"
    assert state["week"]["title"]["challenger_id"] == PLAYER_A
    assert state["week"]["submissions"]["qualifier"][str(PLAYER_A)]["status"] == "confirmed"
    assert state["players"][str(PLAYER_A)]["wins"] == 1


def test_confirm_title_elects_next_leader():
    state = open_week([PLAYER_A])
    _to_title(state)
    ml.submit_result(state, PLAYER_A, "2-1", PROOF, SUNDAY_END)

    status, payload = ml.confirm_result(state, PLAYER_A, SUNDAY_END)

    assert status == "ok"
    assert payload["result"]["leader_changed"] is True
    assert state["week"]["phase"] == "done"
    assert state["leader_elect"] == PLAYER_A
    assert state["leader"]["user_id"] == LEADER

    ml.ensure_week(state, NEXT_MONDAY)
    assert state["leader"]["user_id"] == PLAYER_A


def test_confirm_without_submission():
    state = qualifier_state()
    assert ml.confirm_result(state, PLAYER_A, SUNDAY_DRAW)[0] == "no_submission"


def test_dispute_then_confirm_still_works():
    state = qualifier_state()
    ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)

    status, payload = ml.dispute_result(state, PLAYER_A, PLAYER_B, SUNDAY_DRAW)
    assert status == "ok"
    assert payload["by_id"] == PLAYER_B
    assert state["week"]["submissions"]["qualifier"][str(PLAYER_A)]["status"] == "disputed"

    # Owner vẫn có thể chấp nhận biên bản sau khi bị phản đối.
    status, payload = ml.confirm_result(state, PLAYER_A, SUNDAY_DRAW)
    assert status == "ok"
    assert state["week"]["phase"] == "title"


def test_pending_submission_and_summary():
    state = qualifier_state()
    assert ml.pending_submission(state) is None

    ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)
    pending = ml.pending_submission(state)
    assert pending["submitter_id"] == PLAYER_A
    assert pending["score"] == "1-0"

    summary = ml.week_summary(state, SUNDAY_DRAW)
    assert "Biên bản" in summary and "1-0" in summary


def test_merge_defaults_keeps_threads_and_submissions():
    state = qualifier_state()
    ml.submit_result(state, PLAYER_A, "1-0", PROOF, SUNDAY_DRAW)
    state["week"]["threads"] = {"qualifier": 999}

    again = ml.merge_defaults(state)

    assert again["week"]["threads"]["qualifier"] == 999
    assert again["week"]["submissions"]["qualifier"][str(PLAYER_A)]["score"] == "1-0"


def test_merge_defaults_repairs_broken_submissions():
    state = ml.merge_defaults(
        {"week": {"week_id": "2026-W45", "phase": "qualifier", "submissions": [], "threads": "x"}}
    )
    assert state["week"]["submissions"]["qualifier"] == {}
    assert state["week"]["threads"] == {}


def test_rollover_carries_thread_ids_for_cleanup():
    state = qualifier_state()
    state["week"]["threads"] = {"qualifier": 555}

    actions = ml.ensure_week(state, NEXT_MONDAY)

    assert actions[0][0] == "timeout"
    assert actions[0][1]["threads"] == [555]


def test_merge_defaults_reads_new_fields():
    state = ml.merge_defaults(
        {"leader_elect": "2001", "wl_seq": 7, "week": {"wl": 7, "week_id": "2026-W45"}}
    )
    assert state["leader_elect"] == 2001
    assert state["wl_seq"] == 7
    assert state["week"]["wl"] == 7

    broken = ml.merge_defaults({"leader_elect": True, "wl_seq": -3, "week": {"wl": "x"}})
    assert broken["leader_elect"] is None
    assert broken["wl_seq"] == 0
    assert broken["week"]["wl"] == 0


def test_reset_state_returns_clean_default():
    state = open_week([PLAYER_A])
    state["leader_elect"] = PLAYER_A
    state["wl_seq"] = 5
    state["week"]["threads"] = {"qualifier": 1}

    clean = ml.reset_state()

    assert clean == ml.DEFAULT_STATE
    assert clean["week"]["phase"] == "idle"
    assert clean["leader"]["user_id"] is None
    assert clean["leader_elect"] is None
    assert clean["wl_seq"] == 0
    assert clean["week"]["threads"] == {}
