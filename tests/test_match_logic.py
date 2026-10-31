"""Test luật giải đấu tuần trong `match_logic.py`.

Mốc thời gian dùng xuyên suốt:
    Thứ 2  02/11/2026 00:00  -> mở đăng ký WL1
    Thứ 2  09/11/2026 00:00  -> chốt đăng ký, mở giải (tuần 2026-W46)
    Thứ 5  12/11/2026 00:00  -> hết hạn vòng loại BO1 (chưa báo -> loại cả 2)
    CN     15/11/2026 00:00  -> hết hạn ghép cặp lần 2, random 2 Trainer
    CN     15/11/2026 23:59  -> hết hạn chung kết
    Thứ 2  16/11/2026 00:00  -> người thắng nhậm chức Gym Leader
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
PLAYER_D = 2004
PLAYER_E = 2005


def at(day, hour=0, minute=0, second=0):
    """Giờ Việt Nam trong tháng 11/2026."""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=ml.TZ)


MONDAY = at(2, 0, 1)          # mở đăng ký WL1
NEXT_MONDAY = at(9, 0, 1)     # chốt đăng ký, mở giải
TUESDAY = at(10, 20)
WEDNESDAY = at(11, 12)
THURSDAY = at(12, 0, 1)
FRIDAY = at(13, 12)
SATURDAY = at(14, 12)
SUNDAY = at(15, 0, 1)
SUNDAY_END = at(15, 23, 59, 59)
FOLLOWING_MONDAY = at(16, 0, 1)


def fresh_state(leader_id=LEADER):
    state = ml.merge_defaults({})
    if leader_id:
        start, end = ml.next_term(MONDAY)
        state["leader"] = {
            "user_id": leader_id,
            "term_start": start.isoformat(),
            "term_end": end.isoformat(),
        }
    return state


def signup_week(registrations=(), leader_id=LEADER):
    """Tuần đầu: mở đăng ký WL1 với danh sách cho trước."""
    state = fresh_state(leader_id)
    actions = ml.ensure_week(state, MONDAY)
    assert [kind for kind, _ in actions] == ["open"]
    state["signup"]["registrations"] = list(registrations)
    return state


def start_tournament(registrations, leader_id=LEADER, rng=None):
    """Sang thứ 2 tuần sau: chốt đăng ký và mở giải."""
    state = signup_week(registrations, leader_id)
    actions = ml.ensure_week(state, NEXT_MONDAY, rng=rng or __import__("random").Random(7))
    return state, actions


def kinds(actions):
    return [kind for kind, _ in actions]


def main_kinds(actions):
    """Bỏ qua 'repost' (chỉ là đăng lại panel, không phải bước của giải)."""
    return [kind for kind in kinds(actions) if kind != "repost"]


def open_match(state, phase=None):
    matches = ml.open_matches(state, phase)
    assert matches, "phải có trận đang mở"
    return matches[0]


def resolve(state, match, winner_id, current):
    """Chốt trận bằng đường owner (không cần biên bản)."""
    status, payload = ml.force_result(state, winner_id, current, match["id"])
    assert status == "ok"
    return payload


# --------------------------------------------------------------------------- #
# Mở tuần / đăng ký
# --------------------------------------------------------------------------- #

def test_first_run_opens_wl1_once():
    state = fresh_state()
    actions = ml.ensure_week(state, MONDAY)

    assert kinds(actions) == ["open"]
    assert actions[0][1] == {"wl": 1, "week_id": "2026-W45"}
    assert state["signup"]["wl"] == 1
    assert state["signup"]["open"] is True
    assert state["signup"]["registrations"] == []
    assert state["tournament"]["phase"] == "idle"
    assert ml.ensure_week(state, MONDAY) == []


def test_wl_counter_increments_each_week():
    state = signup_week([PLAYER_A])
    ml.ensure_week(state, NEXT_MONDAY)
    assert state["signup"]["wl"] == 2

    ml.ensure_week(state, at(16, 0, 1))
    assert state["signup"]["wl"] == 3


def test_panel_reposts_every_8_hours():
    state = signup_week([PLAYER_A])
    assert ml.ensure_week(state, MONDAY + timedelta(hours=7, minutes=59)) == []

    actions = ml.ensure_week(state, MONDAY + timedelta(hours=8, minutes=1))
    assert kinds(actions) == ["repost"]

    assert ml.ensure_week(state, MONDAY + timedelta(hours=8, minutes=3)) == []
    actions = ml.ensure_week(state, MONDAY + timedelta(hours=16, minutes=5))
    assert kinds(actions) == ["repost"]


def test_registration_toggle_and_leave():
    state = signup_week()
    assert ml.toggle_registration(state, PLAYER_A, TUESDAY) == ("joined", {"count": 1})
    assert ml.toggle_registration(state, PLAYER_A, TUESDAY) == ("left", {"count": 0})
    ml.toggle_registration(state, PLAYER_A, TUESDAY)

    assert ml.leave_registration(state, PLAYER_A, TUESDAY) == ("left", {"count": 0})
    assert ml.leave_registration(state, PLAYER_A, TUESDAY) == ("absent", {"count": 0})
    assert state["signup"]["registrations"] == []


def test_leader_can_register_to_defend():
    state = signup_week()
    assert ml.toggle_registration(state, LEADER, TUESDAY)[0] == "joined"


# --------------------------------------------------------------------------- #
# Chốt đăng ký -> vòng loại
# --------------------------------------------------------------------------- #

def test_rollover_starts_qualifier_with_random_pairs():
    state, actions = start_tournament([PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D])

    assert kinds(actions) == ["draw_qualifier", "open"]
    draw = actions[0][1]
    assert draw["wl"] == 1
    assert draw["round"] == 1
    assert len(draw["pairs"]) == 2
    assert sorted(uid for pair in draw["pairs"] for uid in pair) == sorted(
        [PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D]
    )
    assert state["tournament"]["phase"] == "qualifier"
    assert len(ml.open_matches(state, "qualifier")) == 2
    assert state["signup"]["wl"] == 2, "WL mới mở song song"


def test_two_registrants_skip_qualifier():
    state, actions = start_tournament([PLAYER_A, PLAYER_B])

    assert kinds(actions) == ["draw_final", "open"]
    assert state["tournament"]["phase"] == "final"
    final = ml.open_matches(state, "final")[0]
    assert final["best_of"] == 3
    assert sorted(final["players"]) == sorted([PLAYER_A, PLAYER_B])


def test_one_registrant_is_elected_directly():
    state, actions = start_tournament([PLAYER_A])

    assert kinds(actions) == ["walkover", "open"]
    assert state["leader_elect"] == PLAYER_A
    assert state["leader"]["user_id"] == LEADER

    actions = ml.ensure_week(state, FOLLOWING_MONDAY)
    assert main_kinds(actions) == ["promote", "no_signup", "open"]
    assert state["leader"]["user_id"] == PLAYER_A


def test_no_registrant_keeps_leader():
    state, actions = start_tournament([])

    assert main_kinds(actions) == ["no_signup", "open"]
    start, end = ml.next_term(MONDAY)
    assert state["leader"]["term_start"] == start.isoformat()
    assert state["leader"]["term_end"] == end.isoformat()


# --------------------------------------------------------------------------- #
# Vòng loại: đúng 1 lượt BO1, không ép giảm về 2 người
# --------------------------------------------------------------------------- #

def test_qualifier_is_single_bo1_round():
    state, _ = start_tournament(
        [PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D, PLAYER_E], rng=__import__("random").Random(3)
    )
    assert state["tournament"]["phase"] == "qualifier"
    assert len(ml.open_matches(state, "qualifier")) == 2

    for match in list(ml.open_matches(state, "qualifier")):
        resolve(state, match, match["players"][0], TUESDAY)

    actions = ml.ensure_week(state, WEDNESDAY)
    assert main_kinds(actions) == ["draw_second"], "không đánh thêm vòng loại"
    assert state["tournament"]["phase"] == "second"
    assert len(state["tournament"]["players"]) == 3, "3 người vào ghép cặp lần 2"
    assert len(ml.open_matches(state, "second")) == 1
    assert len(state["tournament"]["flags"].get("excluded") or []) == 0


def test_qualifier_two_survivors_go_straight_to_final():
    state, _ = start_tournament([PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D])
    for match in list(ml.open_matches(state, "qualifier")):
        resolve(state, match, match["players"][0], TUESDAY)
    survivors = sorted(state["tournament"]["players"])
    assert len(survivors) == 2

    actions = ml.ensure_week(state, WEDNESDAY)

    assert main_kinds(actions) == ["draw_final"]
    final = ml.open_matches(state, "final")[0]
    assert sorted(final["players"]) == survivors


def test_qualifier_winner_advances_and_loser_leaves():
    state, _ = start_tournament([PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D])
    match = open_match(state, "qualifier")
    winner, loser = match["players"]

    resolve(state, match, winner, TUESDAY)

    assert state["players"][str(winner)]["wins"] == 1
    assert state["players"][str(loser)]["losses"] == 1
    assert loser not in state["tournament"]["players"]
    assert state["history"][-1]["type"] == "qualifier"


def test_thursday_deadline_eliminates_both():
    state, _ = start_tournament([PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D, PLAYER_E])
    matches = ml.open_matches(state, "qualifier")
    assert len(matches) == 2
    resolve(state, matches[0], matches[0]["players"][0], TUESDAY)
    winner = matches[0]["players"][0]

    actions = ml.ensure_week(state, THURSDAY)

    assert main_kinds(actions) == ["eliminated", "draw_final"], "còn 2 người -> chung kết"
    eliminated = actions[0][1]["matches"]
    assert len(eliminated) == 1
    assert sorted(eliminated[0]["players"]) == sorted(matches[1]["players"])
    assert len(state["tournament"]["players"]) == 2
    assert winner in state["tournament"]["players"]
    assert state["history"][-1]["type"] == "qualifier_void"
    assert state["tournament"]["phase"] == "final"


def test_thursday_deadline_cancels_when_too_few():
    state, _ = start_tournament([PLAYER_A, PLAYER_B, PLAYER_C])
    for match in list(ml.open_matches(state, "qualifier")):
        # Không ai báo kết quả.
        pass

    actions = ml.ensure_week(state, THURSDAY)

    assert "cancelled" in main_kinds(actions)
    assert state["tournament"]["phase"] == "done"
    assert state["tournament"]["flags"]["cancelled"] is True


# --------------------------------------------------------------------------- #
# Ghép cặp lần 2 -> random 2 Trainer -> chung kết
# --------------------------------------------------------------------------- #

def test_second_round_builds_finalists():
    state, _ = start_tournament(
        [PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D, PLAYER_E], rng=__import__("random").Random(3)
    )
    for match in list(ml.open_matches(state, "qualifier")):
        resolve(state, match, match["players"][0], TUESDAY)
    ml.ensure_week(state, WEDNESDAY)
    pool = list(state["tournament"]["players"])
    assert len(pool) == 3

    match = open_match(state, "second")
    resolve(state, match, match["players"][0], FRIDAY)

    actions = ml.ensure_week(state, FRIDAY)

    assert main_kinds(actions) == ["draw_final"]
    final = ml.open_matches(state, "final")[0]
    assert final["best_of"] == 3
    assert len(final["players"]) == 2
    assert set(final["players"]).issubset(set(pool))
    assert state["tournament"]["phase"] == "final"


def test_second_round_deadline_excludes_unreported():
    players = [PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D, PLAYER_E, 2006, 2007, 2008]
    state, _ = start_tournament(players, rng=__import__("random").Random(3))
    for match in list(ml.open_matches(state, "qualifier")):
        resolve(state, match, match["players"][0], TUESDAY)
    ml.ensure_week(state, WEDNESDAY)
    assert len(state["tournament"]["players"]) == 4
    second = ml.open_matches(state, "second")
    assert len(second) == 2
    resolve(state, second[0], second[0]["players"][0], FRIDAY)

    actions = ml.ensure_week(state, SUNDAY)

    assert main_kinds(actions) == ["eliminated", "draw_final"]
    eliminated = actions[0][1]["matches"]
    assert sorted(eliminated[0]["players"]) == sorted(second[1]["players"])
    final = ml.open_matches(state, "final")[0]
    assert sorted(final["players"]) == sorted(second[0]["players"])
    for uid in second[1]["players"]:
        assert uid not in state["tournament"]["players"]
        assert uid in state["tournament"]["flags"]["excluded"]


def test_priority_goes_to_unpicked_players():
    state = ml.merge_defaults({})
    state["signup"].update({"week_id": ml.week_id(SATURDAY), "open": False})
    state["tournament"].update({
        "wl": 3,
        "week_id": ml.week_id(SATURDAY),
        "phase": "second",
        "start": NEXT_MONDAY.isoformat(),
        "players": [PLAYER_A, PLAYER_B, PLAYER_C],
    })

    actions = ml.ensure_week(state, SATURDAY, rng=_ZeroRng())

    assert main_kinds(actions) == ["draw_final"]
    picked = actions[0][1]["players"]
    skipped = [uid for uid in (PLAYER_A, PLAYER_B, PLAYER_C) if uid not in picked]
    assert len(picked) == 2 and len(skipped) == 1
    assert state["priority"][str(skipped[0])] == 1
    assert all(state["priority"].get(str(uid), 0) == 0 for uid in picked)


def test_priority_raises_chance_to_be_picked():
    state = ml.merge_defaults({})
    state["signup"].update({"week_id": ml.week_id(SATURDAY), "open": False})
    state["tournament"].update({
        "wl": 3,
        "week_id": ml.week_id(SATURDAY),
        "phase": "second",
        "start": NEXT_MONDAY.isoformat(),
        "players": [PLAYER_A, PLAYER_B, PLAYER_C],
    })
    state["priority"] = {str(PLAYER_C): 9}

    actions = ml.ensure_week(state, SATURDAY, rng=_HighRollRng())

    assert PLAYER_C in actions[0][1]["players"]


class _ZeroRng:
    """Luôn chọn phần tử đầu tiên của danh sách còn lại."""

    def random(self):
        return 0.0

    def shuffle(self, items):
        return None


class _HighRollRng:
    """Roll sát cuối -> phần tử có trọng số lớn nhất (priority cao) được chọn."""

    def random(self):
        return 0.999

    def shuffle(self, items):
        return None


# --------------------------------------------------------------------------- #
# Chung kết & nhậm chức
# --------------------------------------------------------------------------- #

def final_state():
    state, _ = start_tournament([PLAYER_A, PLAYER_B])
    assert state["tournament"]["phase"] == "final"
    return state


def test_final_winner_becomes_leader_elect_only():
    state = final_state()
    final = open_match(state, "final")
    winner = final["players"][0]

    resolve(state, final, winner, SUNDAY_END)

    assert state["leader_elect"] == winner
    assert state["leader"]["user_id"] == LEADER, "leader cũ tại vị tới hết Chủ Nhật"
    assert state["tournament"]["phase"] == "done"
    assert state["players"][str(winner)]["titles"] == 0


def test_final_winner_takes_office_at_monday_midnight():
    state = final_state()
    final = open_match(state, "final")
    winner = final["players"][0]
    resolve(state, final, winner, SUNDAY_END)

    actions = ml.ensure_week(state, FOLLOWING_MONDAY)

    assert main_kinds(actions)[0] == "promote"
    assert state["leader"]["user_id"] == winner
    assert state["leader_elect"] is None
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_start"] == start.isoformat()
    assert state["leader"]["term_end"] == end.isoformat()
    assert state["players"][str(winner)]["titles"] == 1


def test_final_timeout_keeps_leader():
    state = final_state()
    actions = ml.ensure_week(state, FOLLOWING_MONDAY)

    # Sang tuần mới mà chung kết chưa có kết quả -> hủy, leader giữ chức.
    assert main_kinds(actions)[0] == "timeout"
    assert state["leader_elect"] is None
    start, end = ml.next_term(SUNDAY_END)
    assert state["leader"]["term_end"] == end.isoformat()


def test_submit_and_confirm_match():
    state = final_state()
    match = open_match(state, "final")
    submitter, opponent = match["players"]

    status, payload = ml.submit_result(
        state, match["id"], submitter, "2-0", {"message_id": 1}, FRIDAY
    )
    assert status == "ok"
    assert payload["winner_id"] == submitter
    assert ml.pending_submission(state)["match_id"] == match["id"]

    status, payload = ml.confirm_result(state, match["id"], submitter, FRIDAY)
    assert status == "ok"
    assert payload["winner_id"] == submitter
    assert match["status"] == "done"
    assert state["players"][str(opponent)]["losses"] == 1


def test_bad_score_and_wrong_player():
    state = final_state()
    match = open_match(state, "final")

    assert ml.submit_result(state, match["id"], PLAYER_A, "1-0", None, FRIDAY)[0] == "bad_score"
    assert ml.submit_result(state, match["id"], PLAYER_E, "2-0", None, FRIDAY)[0] == "not_in_match"
    assert ml.submit_result(state, 999, PLAYER_A, "2-0", None, FRIDAY)[0] == "no_match"


def test_dispute_marks_submission():
    state = final_state()
    match = open_match(state, "final")
    submitter, opponent = match["players"]
    ml.submit_result(state, match["id"], submitter, "2-0", None, FRIDAY)

    status, payload = ml.dispute_result(state, match["id"], submitter, opponent, FRIDAY)

    assert status == "ok"
    assert ml.get_submission(state, match["id"], submitter)["status"] == "disputed"
    assert payload["by_id"] == opponent


def test_force_result_requires_open_match():
    state = final_state()
    match = open_match(state, "final")
    resolve(state, match, match["players"][0], FRIDAY)

    assert ml.force_result(state, PLAYER_E, FRIDAY)[0] == "no_match"
    assert ml.force_result(state, match["players"][0], FRIDAY)[0] == "no_match"


# --------------------------------------------------------------------------- #
# Schema / migration / reset
# --------------------------------------------------------------------------- #

def test_merge_defaults_reads_new_fields():
    state = ml.merge_defaults({
        "leader_elect": "2001",
        "wl_seq": 7,
        "signup": {"wl": 7, "registrations": ["2002"], "open": True},
        "tournament": {"wl": 6, "phase": "final", "players": ["2001", "2002"]},
    })
    assert state["leader_elect"] == 2001
    assert state["wl_seq"] == 7
    assert state["signup"]["registrations"] == [2002]
    assert state["tournament"]["phase"] == "final"


def test_merge_defaults_migrates_old_week_schema():
    state = ml.merge_defaults({
        "week": {
            "week_id": "2026-W45",
            "wl": 4,
            "phase": "open",
            "registrations": [PLAYER_A, PLAYER_B],
            "message_id": 123,
        }
    })
    assert state["signup"]["wl"] == 4
    assert state["signup"]["registrations"] == [PLAYER_A, PLAYER_B]
    assert state["signup"]["message_id"] == 123
    assert state["signup"]["open"] is True


def test_merge_defaults_repairs_broken_matches():
    state = ml.merge_defaults({
        "tournament": {
            "phase": "khong-hop-le",
            "matches": {
                "1": {"id": 1, "players": [PLAYER_A]},
                "2": {"id": 2, "players": [PLAYER_A, PLAYER_B], "phase": "final"},
            },
        }
    })
    assert state["tournament"]["phase"] == "idle"
    assert list(state["tournament"]["matches"]) == ["2"]
    assert state["tournament"]["matches"]["2"]["best_of"] == 1


def test_merge_defaults_migrates_legacy_history():
    state = ml.merge_defaults({
        "match_history": [
            {"match_id": 9, "challenger_id": 1, "owner_id": 2, "result": "thang", "ended_at": "x"}
        ]
    })
    assert state["history"][0]["type"] == "legacy"
    assert state["history"][0]["winner_id"] == 1


def test_reset_state_returns_clean_default():
    state, _ = start_tournament([PLAYER_A, PLAYER_B])
    state["leader_elect"] = PLAYER_A
    state["wl_seq"] = 5

    clean = ml.reset_state()

    assert clean == ml.DEFAULT_STATE
    assert clean["signup"]["open"] is False
    assert clean["tournament"]["phase"] == "idle"
    assert clean["leader"]["user_id"] is None
    assert clean["wl_seq"] == 0


def test_start_tournament_now_for_testing():
    state = signup_week([PLAYER_A, PLAYER_B, PLAYER_C])
    status, actions = ml.start_tournament_now(state, WEDNESDAY)

    assert status == "ok"
    assert kinds(actions) == ["draw_qualifier", "open"]
    assert state["tournament"]["phase"] == "qualifier"
    assert state["signup"]["wl"] == 2
    assert state["tournament"]["start"] == WEDNESDAY.isoformat()

    assert ml.start_tournament_now(state, WEDNESDAY)[0] == "busy"


def test_start_tournament_now_needs_two_players():
    state = signup_week([PLAYER_A])
    assert ml.start_tournament_now(state, WEDNESDAY)[0] == "few"


def test_tournament_summary_mentions_open_match():
    state, _ = start_tournament([PLAYER_A, PLAYER_B])
    summary = ml.week_summary(state, FRIDAY)

    assert "WL1" in summary
    assert f"<@{PLAYER_A}>" in summary or f"<@{PLAYER_B}>" in summary
    assert "Hạn chót" in summary
