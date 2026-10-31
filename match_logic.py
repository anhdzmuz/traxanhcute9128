"""Luật giải đấu hàng tuần của Gym Dark (TGA).

Module này cố ý KHÔNG import discord hay psycopg: mọi hàm chỉ nhận dict `state`
(trạng thái đầy đủ) và thời gian, rồi sửa trực tiếp dict đó. Nhờ vậy:

* có thể chạy trong `update_data(...)` (khóa dòng DB) mà không có `await` bên trong;
* có thể unit test mà không cần Discord hay Postgres.

Mỗi giải chạy trong 1 tuần (giờ Việt Nam):

    Thứ 2 00:00    chốt đăng ký của tuần trước, mở giải mới (WL mới mở đăng ký)
    Thứ 2 - Thứ 4  vòng loại BO1: ghép cặp ngẫu nhiên, mỗi người đấu 1 trận
    Thứ 5 00:00    hết hạn vòng loại: cặp nào chưa báo kết quả -> loại CẢ 2
    Thứ 5 - Thứ 7  ghép cặp lần 2 giữa những người còn lại để chốt danh sách random
    CN    00:00    hết hạn ghép cặp lần 2 -> random 2 Trainer vào chung kết
    CN             chung kết BO3
    Thứ 2 00:00    người thắng chung kết nhậm chức Gym Leader

Ai vào được vòng Thứ 5 - Thứ 7 mà không được random chọn thì được +1 ưu tiên
cho lần sau (xem `_pick_finalists`).
"""

import copy
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

REPOST_HOURS = 8          # đăng lại panel đăng ký mỗi 8 giờ (im lặng)
PING_HOUR = 18            # nhắc mỗi ngày lúc 18:00 khi giải đang chạy
QUALIFIER_BEST_OF = 1     # vòng loại BO1
SECOND_BEST_OF = 1        # ghép cặp lần 2 cũng BO1
FINAL_BEST_OF = 3         # chung kết BO3
MIN_PLAYERS = 3           # từ 3 người mới chạy vòng loại
MAX_HISTORY = 200

QUALIFIER_DAYS = 3        # Thứ 2 00:00 -> Thứ 5 00:00
SECOND_DAYS = 6           # Thứ 2 00:00 -> Chủ Nhật 00:00
FINAL_DAYS = 7            # Thứ 2 00:00 -> Thứ 2 tuần sau 00:00

PHASES = ("idle", "qualifier", "second", "final", "done")

DEFAULT_STATE = {
    "leader": {"user_id": None, "term_start": None, "term_end": None},
    # Người thắng chung kết: chỉ nhậm chức vào 00:00 thứ 2 tuần sau.
    "leader_elect": None,
    "wl_seq": 0,              # số thứ tự tuần giải đấu (WL1, WL2, ...)
    "signup": {               # đăng ký đang mở cho giải kế tiếp
        "wl": 0,
        "week_id": None,
        "registrations": [],
        "message_id": None,
        "repost_at": None,
        "open": False,
    },
    "tournament": {           # giải đang chạy
        "wl": 0,
        "week_id": None,
        "phase": "idle",      # idle | qualifier | second | final | done
        "start": None,        # thứ 2 00:00 của tuần giải chạy
        "round": 0,
        "players": [],        # người còn lại
        "matches": {},        # str(match_id) -> match
        "final_id": None,
        "flags": {},
    },
    "priority": {},
    "players": {},
    "history": [],
    "next_match_id": 1,
}


# --------------------------------------------------------------------------- #
# Thời gian
# --------------------------------------------------------------------------- #

def now():
    return datetime.now(TZ)


def parse_dt(value):
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def format_remaining(delta):
    seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days} ngày {hours} giờ"
    if hours:
        return f"{hours} giờ {minutes} phút"
    return f"{minutes} phút"


def week_id(moment):
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(moment):
    """Thứ 2 00:00 của tuần chứa `moment`."""
    start = moment - timedelta(days=moment.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def signup_close_at(moment):
    """Thứ 2 00:00 tuần sau — lúc chốt đăng ký và mở giải."""
    return week_start(moment) + timedelta(days=7)


def next_term(current):
    """Nhiệm kỳ leader: từ thứ 2 tuần sau đến hết Chủ Nhật tuần sau."""
    start = week_start(current) + timedelta(days=7)
    end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return start, end


def _tournament_start(tournament):
    try:
        return parse_dt(tournament["start"])
    except (KeyError, TypeError, ValueError):
        return None


def qualifier_end(tournament):
    start = _tournament_start(tournament)
    return start + timedelta(days=QUALIFIER_DAYS) if start else None


def second_end(tournament):
    start = _tournament_start(tournament)
    return start + timedelta(days=SECOND_DAYS) if start else None


def final_end(tournament):
    start = _tournament_start(tournament)
    return start + timedelta(days=FINAL_DAYS) if start else None


def tournament_deadline(tournament):
    """Hạn chót của vòng đang chạy (None nếu không có trận nào)."""
    return {
        "qualifier": qualifier_end,
        "second": second_end,
        "final": final_end,
    }.get(tournament.get("phase"), lambda _t: None)(tournament)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def _migrate_legacy_history(items):
    """Chuyển match_history của hệ thống 3 tầng cũ sang lịch sử mới."""
    migrated = []
    for item in items:
        if not isinstance(item, dict):
            continue
        challenger = item.get("challenger_id")
        owner = item.get("owner_id")
        if challenger is None or owner is None:
            continue
        challenger_won = item.get("result") == "thang"
        migrated.append({
            "match_id": item.get("match_id"),
            "type": "legacy",
            "week": "",
            "winner_id": challenger if challenger_won else owner,
            "loser_id": owner if challenger_won else challenger,
            "at": item.get("ended_at", ""),
        })
    return migrated[-MAX_HISTORY:]


def _as_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _clean_matches(raw):
    cleaned = {}
    if not isinstance(raw, dict):
        return cleaned
    for key, match in raw.items():
        if not isinstance(match, dict):
            continue
        players = [_as_id(uid) for uid in (match.get("players") or [])]
        players = [uid for uid in players if uid]
        if len(players) != 2:
            continue
        mid = _as_id(match.get("id")) or _as_id(key)
        if not mid:
            continue
        subs = match.get("submissions")
        cleaned[str(mid)] = {
            "id": mid,
            "phase": match.get("phase") if match.get("phase") in ("qualifier", "second", "final") else "qualifier",
            "round": int(match.get("round") or 1),
            "players": players,
            "best_of": int(match.get("best_of") or 1),
            "status": match.get("status") if match.get("status") in ("open", "done", "void") else "open",
            "thread_id": match.get("thread_id"),
            "submissions": {
                str(k): v for k, v in subs.items() if isinstance(v, dict)
            } if isinstance(subs, dict) else {},
            "created_at": match.get("created_at"),
            "ended_at": match.get("ended_at"),
            "winner_id": _as_id(match.get("winner_id")),
            "loser_id": _as_id(match.get("loser_id")),
        }
    return cleaned


def merge_defaults(data):
    """Bổ sung trường mới, giữ dữ liệu cũ có thể chuyển đổi được."""
    state = copy.deepcopy(DEFAULT_STATE)
    if not isinstance(data, dict):
        return state

    leader = data.get("leader")
    if isinstance(leader, dict):
        state["leader"].update({
            "user_id": _as_id(leader.get("user_id")),
            "term_start": leader.get("term_start"),
            "term_end": leader.get("term_end"),
        })

    elect = _as_id(data.get("leader_elect"))
    if elect:
        state["leader_elect"] = elect

    seq = data.get("wl_seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
        state["wl_seq"] = seq

    signup = data.get("signup")
    if isinstance(signup, dict):
        regs = signup.get("registrations")
        state["signup"].update({
            "wl": _as_id(signup.get("wl")) or 0,
            "week_id": signup.get("week_id"),
            "registrations": [_as_id(u) for u in regs if _as_id(u)] if isinstance(regs, list) else [],
            "message_id": _as_id(signup.get("message_id")),
            "repost_at": signup.get("repost_at"),
            "open": bool(signup.get("open")),
        })
    else:
        # Dữ liệu của bản trước (một `week` gộp đăng ký + trận): giữ phần đăng ký.
        week = data.get("week")
        if isinstance(week, dict):
            regs = week.get("registrations")
            state["signup"].update({
                "wl": _as_id(week.get("wl")) or 0,
                "week_id": week.get("week_id"),
                "registrations": [_as_id(u) for u in regs if _as_id(u)] if isinstance(regs, list) else [],
                "message_id": _as_id(week.get("message_id")),
                "repost_at": None,
                "open": week.get("phase") == "open",
            })

    tournament = data.get("tournament")
    if isinstance(tournament, dict):
        players = [_as_id(uid) for uid in (tournament.get("players") or [])]
        flags = tournament.get("flags")
        state["tournament"].update({
            "wl": _as_id(tournament.get("wl")) or 0,
            "week_id": tournament.get("week_id"),
            "phase": tournament.get("phase") if tournament.get("phase") in PHASES else "idle",
            "start": tournament.get("start"),
            "round": int(tournament.get("round") or 0),
            "players": [uid for uid in players if uid],
            "matches": _clean_matches(tournament.get("matches")),
            "final_id": _as_id(tournament.get("final_id")),
            "flags": flags if isinstance(flags, dict) else {},
        })

    if state["wl_seq"] < state["signup"]["wl"]:
        state["wl_seq"] = state["signup"]["wl"]

    if isinstance(data.get("priority"), dict):
        state["priority"] = {
            str(k): int(v) for k, v in data["priority"].items() if str(v).lstrip("-").isdigit()
        }

    if isinstance(data.get("players"), dict):
        for uid, player in data["players"].items():
            if not isinstance(player, dict):
                continue
            state["players"][str(uid)] = {
                "wins": int(player.get("wins", 0) or 0),
                "losses": int(player.get("losses", 0) or 0),
                "titles": int(player.get("titles", 0) or 0),
                "streak": int(player.get("streak", 0) or 0),
                "best_streak": int(player.get("best_streak", 0) or 0),
            }

    history = data.get("history")
    if isinstance(history, list):
        state["history"] = [h for h in history if isinstance(h, dict)][-MAX_HISTORY:]
    elif isinstance(data.get("match_history"), list):
        state["history"] = _migrate_legacy_history(data["match_history"])

    if isinstance(data.get("next_match_id"), int) and data["next_match_id"] > 0:
        state["next_match_id"] = data["next_match_id"]

    return state


def get_player(state, user_id):
    key = str(user_id)
    player = state["players"].setdefault(key, {})
    for field in ("wins", "losses", "titles", "streak", "best_streak"):
        player.setdefault(field, 0)
    return player


def _bump(state, user_id, field):
    player = get_player(state, user_id)
    player[field] = player.get(field, 0) + 1
    return player


def _push_history(state, entry):
    entry["match_id"] = state["next_match_id"]
    state["next_match_id"] = int(state["next_match_id"]) + 1
    state["history"].append(entry)
    state["history"] = state["history"][-MAX_HISTORY:]
    return entry["match_id"]


def _crown(state, user_id, current):
    start, end = next_term(current)
    state["leader"] = {
        "user_id": int(user_id),
        "term_start": start.isoformat(),
        "term_end": end.isoformat(),
    }
    _bump(state, user_id, "titles")


def promote_elect(state, current):
    """Đưa người thắng chung kết lên làm Gym Leader khi bước sang tuần mới.

    `current` phải là mốc cuối tuần trước (Chủ Nhật 23:59:59), để nhiệm kỳ
    rơi đúng vào tuần mới bắt đầu ngay sau đó.
    """
    elect = state.get("leader_elect")
    if not elect:
        return None
    previous = state["leader"].get("user_id")
    _crown(state, elect, current)
    if previous == elect:
        # Bảo vệ chức: không tính thêm một danh hiệu mới.
        player = get_player(state, elect)
        player["titles"] = max(0, player.get("titles", 0) - 1)
    state["leader_elect"] = None
    return int(elect)


def extend_term(state, current):
    if not state["leader"].get("user_id"):
        return
    start, end = next_term(current)
    state["leader"]["term_start"] = start.isoformat()
    state["leader"]["term_end"] = end.isoformat()


# --------------------------------------------------------------------------- #
# Đăng ký
# --------------------------------------------------------------------------- #

def toggle_registration(state, user_id, current):
    """Bật/tắt đăng ký. Trả về (trạng_thái, dữ_liệu)."""
    signup = state["signup"]
    if not signup.get("open"):
        return "closed", None
    registrations = signup["registrations"]
    if user_id in registrations:
        registrations.remove(user_id)
        return "left", {"count": len(registrations)}
    registrations.append(int(user_id))
    return "joined", {"count": len(registrations)}


def leave_registration(state, user_id, current):
    """Hủy đăng ký (không bao giờ tự đăng ký lại như `toggle_registration`)."""
    signup = state["signup"]
    if not signup.get("open"):
        return "closed", None
    registrations = signup["registrations"]
    if user_id not in registrations:
        return "absent", {"count": len(registrations)}
    registrations.remove(user_id)
    return "left", {"count": len(registrations)}


# --------------------------------------------------------------------------- #
# Trận đấu
# --------------------------------------------------------------------------- #

def get_match(state, match_id):
    try:
        return state["tournament"]["matches"].get(str(int(match_id)))
    except (TypeError, ValueError):
        return None


def open_matches(state, phase=None):
    matches = [
        m for m in (state["tournament"].get("matches") or {}).values()
        if m.get("status") == "open" and (phase is None or m.get("phase") == phase)
    ]
    return sorted(matches, key=lambda m: m["id"])


def match_for_thread(state, thread_id):
    if not thread_id:
        return None
    for match in (state["tournament"].get("matches") or {}).values():
        if match.get("thread_id") == thread_id:
            return match
    return None


def match_for_user(state, user_id):
    for match in open_matches(state):
        if user_id in match["players"]:
            return match
    return None


def all_thread_ids(state):
    return [
        m.get("thread_id") for m in (state["tournament"].get("matches") or {}).values()
        if m.get("thread_id")
    ]


def _new_match(state, phase, round_no, players, best_of, current):
    tournament = state["tournament"]
    match_id = int(state["next_match_id"])
    state["next_match_id"] = match_id + 1
    match = {
        "id": match_id,
        "phase": phase,
        "round": int(round_no),
        "players": list(players),
        "best_of": best_of,
        "status": "open",
        "thread_id": None,
        "submissions": {},
        "created_at": current.isoformat(),
        "ended_at": None,
        "winner_id": None,
        "loser_id": None,
    }
    tournament["matches"][str(match_id)] = match
    return match


def _pair_round(state, phase, best_of, current, rng):
    """Chia cặp ngẫu nhiên cho vòng mới; người lẻ được đi tiếp (bye)."""
    tournament = state["tournament"]
    alive = list(tournament["players"])
    rng.shuffle(alive)
    tournament["round"] = int(tournament.get("round") or 0) + 1
    pairs = []
    while len(alive) >= 2:
        pairs.append([alive.pop(), alive.pop()])
    byes = alive
    match_ids = [
        _new_match(state, phase, tournament["round"], pair, best_of, current)["id"]
        for pair in pairs
    ]
    return {
        "wl": tournament["wl"],
        "phase": phase,
        "round": tournament["round"],
        "pairs": pairs,
        "byes": byes,
        "match_ids": match_ids,
    }


def _draw_qualifier(state, current, rng):
    return _pair_round(state, "qualifier", QUALIFIER_BEST_OF, current, rng)


def _draw_second(state, current, rng):
    return _pair_round(state, "second", SECOND_BEST_OF, current, rng)


def _start_second(state, current, rng):
    tournament = state["tournament"]
    tournament["phase"] = "second"
    tournament["round"] = 0
    return _draw_second(state, current, rng)


def _weighted_pick(pool, priority, count, rng):
    """Random có trọng số: priority càng cao càng dễ được chọn."""
    remaining = list(pool)
    picked = []
    while remaining and len(picked) < count:
        weights = [1 + max(0, int(priority.get(str(uid), 0))) for uid in remaining]
        roll = rng.random() * sum(weights)
        total = 0
        chosen = remaining[-1]
        for uid, weight in zip(remaining, weights):
            total += weight
            if roll <= total:
                chosen = uid
                break
        picked.append(chosen)
        remaining.remove(chosen)
    return picked


def _final_pool(state):
    tournament = state["tournament"]
    excluded = tournament["flags"].get("excluded") or []
    return [uid for uid in tournament["players"] if uid not in excluded]


def _pick_finalists(state, current, rng):
    """Chọn 2 Trainer vào chung kết; người không được chọn +1 ưu tiên."""
    tournament = state["tournament"]
    pool = _final_pool(state)
    if len(pool) < 2:
        return None
    picked = list(pool) if len(pool) == 2 else _weighted_pick(pool, state["priority"], 2, rng)
    for uid in pool:
        key = str(uid)
        if uid in picked:
            state["priority"][key] = 0
        else:
            state["priority"][key] = int(state["priority"].get(key, 0)) + 1
    match = _new_match(state, "final", 1, picked, FINAL_BEST_OF, current)
    tournament["phase"] = "final"
    tournament["final_id"] = match["id"]
    return {
        "wl": tournament["wl"],
        "players": picked,
        "pool": pool,
        "match_id": match["id"],
    }


def _next_after_qualifier(state, current, rng):
    """Sau vòng loại: còn từ 3 người → ghép cặp lần 2; đúng 2 → chung kết luôn."""
    if len(state["tournament"]["players"]) >= 3:
        return ("draw_second", _start_second(state, current, rng))
    return ("draw_final", _pick_finalists(state, current, rng))


def _void_open_matches(state, current, phase):
    """Hết hạn mà chưa có kết quả: loại cả 2 người của mỗi cặp."""
    tournament = state["tournament"]
    voided = []
    for match in open_matches(state, phase):
        match["status"] = "void"
        match["ended_at"] = current.isoformat()
        voided.append({
            "match_id": match["id"],
            "players": list(match["players"]),
            "thread_id": match.get("thread_id"),
        })
        for uid in match["players"]:
            if uid in tournament["players"]:
                tournament["players"].remove(uid)
            get_player(state, uid)["streak"] = 0
            if phase == "second":
                excluded = tournament["flags"].setdefault("excluded", [])
                if uid not in excluded:
                    excluded.append(uid)
        _push_history(state, {
            "type": f"{phase}_void",
            "wl": tournament["wl"],
            "week": tournament["week_id"],
            "winner_id": None,
            "loser_id": None,
            "players": list(match["players"]),
            "at": current.isoformat(),
        })
    return voided


def _cancel_tournament(state, current):
    tournament = state["tournament"]
    for match in (tournament.get("matches") or {}).values():
        if match.get("status") == "open":
            match["status"] = "void"
            match["ended_at"] = current.isoformat()
    if tournament["phase"] != "idle":
        tournament["phase"] = "done"
    tournament["flags"]["cancelled"] = True


def _finish_match(state, match, winner_id, current):
    tournament = state["tournament"]
    loser_id = next(uid for uid in match["players"] if uid != winner_id)
    match["status"] = "done"
    match["winner_id"] = int(winner_id)
    match["loser_id"] = int(loser_id)
    match["ended_at"] = current.isoformat()

    winner = _bump(state, winner_id, "wins")
    winner["streak"] = int(winner.get("streak", 0)) + 1
    winner["best_streak"] = max(int(winner.get("best_streak", 0)), winner["streak"])
    loser = get_player(state, loser_id)
    loser["losses"] = int(loser.get("losses", 0)) + 1
    loser["streak"] = 0

    _push_history(state, {
        "type": match["phase"],
        "wl": tournament["wl"],
        "week": tournament["week_id"],
        "winner_id": int(winner_id),
        "loser_id": int(loser_id),
        "at": current.isoformat(),
    })

    if match["phase"] == "qualifier":
        if loser_id in tournament["players"]:
            tournament["players"].remove(loser_id)
    elif match["phase"] == "final":
        # Chưa đổi leader ngay: nhậm chức vào 00:00 thứ 2 (xem `promote_elect`).
        state["leader_elect"] = int(winner_id)
        tournament["phase"] = "done"
        tournament["flags"]["winner_id"] = int(winner_id)
    return loser_id


# --------------------------------------------------------------------------- #
# Nộp – xác nhận kết quả (trong chat riêng của trận)
# --------------------------------------------------------------------------- #

SCORE_PATTERN = re.compile(r"^\s*(\d)\s*[-:]\s*(\d)\s*$")

VALID_SCORES = {
    1: {(1, 0), (0, 1)},
    3: {(2, 0), (2, 1), (0, 2), (1, 2)},
}


def parse_score(text, best_of):
    """Đọc tỷ số theo góc nhìn người nộp: "2-1" = tôi thắng 2-1."""
    match = SCORE_PATTERN.match(text or "")
    if not match:
        return None
    mine, theirs = int(match.group(1)), int(match.group(2))
    valid = VALID_SCORES.get(best_of, set())
    return (mine, theirs) if (mine, theirs) in valid else None


def get_submission(state, match_id, user_id):
    match = get_match(state, match_id)
    if not isinstance(match, dict):
        return None
    return (match.get("submissions") or {}).get(str(user_id))


def pending_submission(state, match_id=None):
    """Biên bản đang chờ xác nhận, của một trận hoặc của cả giải."""
    matches = [get_match(state, match_id)] if match_id else open_matches(state)
    for match in matches:
        if not isinstance(match, dict):
            continue
        for user_id, sub in (match.get("submissions") or {}).items():
            if isinstance(sub, dict) and sub.get("status") in ("pending", "disputed"):
                return {"match_id": match["id"], "submitter_id": int(user_id), **sub}
    return None


def submit_result(state, match_id, user_id, score, proof, current):
    """Ghi biên bản do một người chơi nộp. Người còn lại sẽ xác nhận."""
    match = get_match(state, match_id)
    if not isinstance(match, dict):
        return "no_match", None
    if match.get("status") != "open":
        return "closed", None
    if user_id not in match["players"]:
        return "not_in_match", None

    best_of = int(match.get("best_of") or 1)
    parsed = parse_score(score, best_of)
    if parsed is None:
        return "bad_score", {"best_of": best_of}

    mine, theirs = parsed
    opponent = next(uid for uid in match["players"] if uid != user_id)
    winner = user_id if mine > theirs else opponent

    submissions = match.setdefault("submissions", {})
    existing = submissions.get(str(user_id))
    if isinstance(existing, dict) and existing.get("status") == "confirmed":
        return "already", None

    submissions[str(user_id)] = {
        "score": f"{mine}-{theirs}",
        "winner_id": int(winner),
        "opponent_id": int(opponent),
        "proof": proof,
        "at": current.isoformat(),
        "status": "pending",
    }
    return "ok", {
        "match_id": match["id"],
        "phase": match["phase"],
        "submitter_id": int(user_id),
        "opponent_id": int(opponent),
        "winner_id": int(winner),
        "score": f"{mine}-{theirs}",
        "best_of": best_of,
    }


def confirm_result(state, match_id, submitter_id, current):
    """Đối thủ (hoặc owner) xác nhận biên bản → ghi kết quả thật."""
    match = get_match(state, match_id)
    if not isinstance(match, dict):
        return "no_match", None
    if match.get("status") != "open":
        return "closed", None
    submission = (match.get("submissions") or {}).get(str(submitter_id))
    if not isinstance(submission, dict):
        return "no_submission", None
    if submission.get("status") == "confirmed":
        return "already", None

    submission["status"] = "confirmed"
    submission["confirmed_at"] = current.isoformat()
    loser_id = _finish_match(state, match, submission["winner_id"], current)
    return "ok", {
        "match_id": match["id"],
        "phase": match["phase"],
        "submitter_id": int(submitter_id),
        "winner_id": submission["winner_id"],
        "loser_id": loser_id,
        "score": submission.get("score"),
        "submission": submission,
    }


def dispute_result(state, match_id, submitter_id, by_id, current):
    """Người còn lại nói kết quả không đúng → chuyển cho owner quyết định."""
    match = get_match(state, match_id)
    if not isinstance(match, dict):
        return "no_match", None
    submission = (match.get("submissions") or {}).get(str(submitter_id))
    if not isinstance(submission, dict):
        return "no_submission", None
    if submission.get("status") == "confirmed":
        return "already", None
    submission["status"] = "disputed"
    submission["disputed_by"] = int(by_id)
    submission["disputed_at"] = current.isoformat()
    return "ok", {
        "match_id": match["id"],
        "phase": match["phase"],
        "submitter_id": int(submitter_id),
        "by_id": int(by_id),
        "score": submission.get("score"),
    }


def force_result(state, winner_id, current, match_id=None):
    """Owner chốt tay người thắng (dùng khi tranh chấp hoặc thiếu biên bản)."""
    if match_id:
        match = get_match(state, match_id)
    else:
        candidates = [m for m in open_matches(state) if winner_id in m["players"]]
        match = candidates[0] if candidates else None
    if not isinstance(match, dict):
        return "no_match", None
    if match.get("status") != "open":
        return "closed", None
    if winner_id not in match["players"]:
        return "not_in_match", None
    loser_id = _finish_match(state, match, winner_id, current)
    return "ok", {
        "match_id": match["id"],
        "phase": match["phase"],
        "winner_id": int(winner_id),
        "loser_id": loser_id,
    }


# --------------------------------------------------------------------------- #
# Vòng đời giải
# --------------------------------------------------------------------------- #

def reset_state():
    """Trạng thái sạch như lần đầu chạy bot (dùng cho lệnh /reset)."""
    return copy.deepcopy(DEFAULT_STATE)


def _fresh_tournament(state, wl, week, start):
    state["tournament"] = copy.deepcopy(DEFAULT_STATE["tournament"])
    state["tournament"].update({
        "wl": int(wl),
        "week_id": week,
        "start": start.isoformat() if start else None,
        "flags": {},
    })
    return state["tournament"]


def _open_signup(state, current):
    state["wl_seq"] = int(state.get("wl_seq") or 0) + 1
    state["signup"] = {
        "wl": state["wl_seq"],
        "week_id": week_id(current),
        "registrations": [],
        "message_id": None,
        "repost_at": current.isoformat(),
        "open": True,
    }
    return {"wl": state["wl_seq"], "week_id": week_id(current)}


def force_open_signup(state, current):
    """[OWNER] Mở lại đăng ký của tuần hiện tại (dùng cho /tao_dang_ky).

    Không đụng tới giải đang chạy; chỉ đảm bảo panel đăng ký dùng được ngay,
    kể cả khi vừa /reset hoặc bot vừa khởi động lại giữa tuần.
    """
    signup = state["signup"]
    if not signup.get("week_id"):
        # Trạng thái mới tinh (sau /reset): mở luôn WL đầu tiên.
        if not signup.get("wl"):
            state["wl_seq"] = int(state.get("wl_seq") or 0) + 1
            signup["wl"] = state["wl_seq"]
        signup["week_id"] = week_id(current)
    signup["open"] = True
    signup["repost_at"] = current.isoformat()
    signup.setdefault("registrations", [])
    return state


def _begin_tournament(state, registrations, current, rng, wl):
    """Chốt danh sách đăng ký và mở giải. Trả về danh sách action."""
    actions = []
    tournament = _fresh_tournament(state, wl, week_id(current), week_start(current))
    if len(registrations) >= MIN_PLAYERS:
        tournament["phase"] = "qualifier"
        tournament["players"] = list(registrations)
        actions.append(("draw_qualifier", _draw_qualifier(state, current, rng)))
    elif len(registrations) == 2:
        # Chỉ 2 người: không cần vòng loại, vào thẳng chung kết BO3.
        tournament["players"] = list(registrations)
        actions.append(("draw_final", _pick_finalists(state, current, rng)))
    elif len(registrations) == 1:
        tournament["phase"] = "done"
        state["leader_elect"] = int(registrations[0])
        actions.append(("walkover", {"wl": wl, "user_id": int(registrations[0])}))
    else:
        tournament["phase"] = "idle"
        actions.append(("no_signup", {"wl": wl}))
    return actions


def _rollover(state, current, rng):
    """Thứ 2 00:00: trao chức, chốt đăng ký cũ, mở giải mới và panel WL mới."""
    actions = []
    signup = state["signup"]
    closing = week_start(current) - timedelta(seconds=1)
    previous_message = signup.get("message_id")
    previous_wl = int(signup.get("wl") or 0)
    registrations = [int(uid) for uid in dict.fromkeys(signup.get("registrations") or [])]

    promoted = promote_elect(state, closing)
    if promoted:
        actions.append(("promote", {"user_id": promoted, "wl": state["tournament"].get("wl") or previous_wl}))
    else:
        extend_term(state, closing)

    if state["tournament"]["phase"] in ("qualifier", "second", "final"):
        threads = all_thread_ids(state)
        wl = state["tournament"].get("wl")
        _cancel_tournament(state, closing)
        actions.append(("timeout", {"wl": wl, "threads": threads}))

    if previous_message:
        actions.append(("close_signup", {"message_id": previous_message}))

    if previous_wl > 0:
        actions.extend(_begin_tournament(state, registrations, current, rng, previous_wl))
    actions.append(("open", _open_signup(state, current)))
    return actions


def ensure_week(state, current, rng=random):
    """Bước tiến của giải. Trả về danh sách action để cog gửi tin nhắn.

    Idempotent: gọi lại nhiều lần trong cùng một mốc thời gian không tạo
    thông báo trùng, nhờ `phase`, trạng thái trận và cờ trong `flags`.
    """
    actions = []
    signup = state["signup"]

    if signup.get("week_id") != week_id(current):
        return _rollover(state, current, rng)

    tournament = state["tournament"]
    phase = tournament["phase"]

    if phase == "qualifier":
        deadline = qualifier_end(tournament)
        if deadline and current >= deadline:
            voided = _void_open_matches(state, current, "qualifier")
            if voided:
                actions.append(("eliminated", {"wl": tournament["wl"], "phase": "qualifier", "matches": voided}))
            if len(tournament["players"]) >= 2:
                actions.append(_next_after_qualifier(state, current, rng))
            else:
                threads = all_thread_ids(state)
                _cancel_tournament(state, current)
                actions.append(("cancelled", {"wl": tournament["wl"], "threads": threads}))
        elif not open_matches(state, "qualifier"):
            # Vòng loại chỉ một lượt BO1: ai thắng (hoặc lẻ cặp được đi tiếp)
            # vào danh sách, không ép giảm về đúng 2 người.
            if len(tournament["players"]) >= 2:
                actions.append(_next_after_qualifier(state, current, rng))
            else:
                threads = all_thread_ids(state)
                _cancel_tournament(state, current)
                actions.append(("cancelled", {"wl": tournament["wl"], "threads": threads}))

    elif phase == "second":
        deadline = second_end(tournament)
        if deadline and current >= deadline:
            voided = _void_open_matches(state, current, "second")
            if voided:
                actions.append(("eliminated", {"wl": tournament["wl"], "phase": "second", "matches": voided}))
            payload = _pick_finalists(state, current, rng)
            if payload:
                actions.append(("draw_final", payload))
            else:
                threads = all_thread_ids(state)
                _cancel_tournament(state, current)
                actions.append(("cancelled", {"wl": tournament["wl"], "threads": threads}))
        elif not open_matches(state, "second"):
            payload = _pick_finalists(state, current, rng)
            if payload:
                actions.append(("draw_final", payload))
            else:
                threads = all_thread_ids(state)
                _cancel_tournament(state, current)
                actions.append(("cancelled", {"wl": tournament["wl"], "threads": threads}))

    elif phase == "final":
        deadline = final_end(tournament)
        if deadline and current >= deadline and open_matches(state, "final"):
            voided = _void_open_matches(state, current, "final")
            threads = all_thread_ids(state)
            _cancel_tournament(state, current)
            actions.append(("final_timeout", {"wl": tournament["wl"], "threads": threads, "matches": voided}))

    if state["tournament"]["phase"] in ("qualifier", "second", "final"):
        day_key = current.strftime("%Y-%m-%d")
        if current.hour >= PING_HOUR and state["tournament"]["flags"].get("ping_day") != day_key:
            state["tournament"]["flags"]["ping_day"] = day_key
            actions.append(("ping", {"wl": state["tournament"]["wl"], "day": day_key}))

    if signup.get("open"):
        last = signup.get("repost_at")
        try:
            due = parse_dt(last) + timedelta(hours=REPOST_HOURS) if last else current
        except (TypeError, ValueError):
            due = current
        if current >= due:
            signup["repost_at"] = current.isoformat()
            actions.append(("repost", {"wl": signup["wl"]}))

    return actions


def start_tournament_now(state, current, rng=random):
    """[OWNER] Chốt đăng ký ngay và mở giải luôn (dùng để test / khi bot trễ)."""
    if state["tournament"]["phase"] in ("qualifier", "second", "final"):
        return "busy", None
    registrations = [int(uid) for uid in dict.fromkeys(state["signup"].get("registrations") or [])]
    if len(registrations) < 2:
        return "few", {"count": len(registrations)}

    signup = state["signup"]
    actions = []
    if signup.get("message_id"):
        actions.append(("close_signup", {"message_id": signup["message_id"]}))
    wl = int(signup.get("wl") or 0)
    actions.extend(_begin_tournament(state, registrations, current, rng, wl))
    # Mốc thời gian tính từ lúc bấm, để các vòng có đủ ngày.
    state["tournament"]["start"] = current.isoformat()
    actions.append(("open", _open_signup(state, current)))
    return "ok", actions


# --------------------------------------------------------------------------- #
# Tiện ích hiển thị
# --------------------------------------------------------------------------- #

def phase_label(phase):
    return {
        "idle": "Chưa có giải",
        "qualifier": "Vòng loại BO1 (Thứ 2 – Thứ 4)",
        "second": "Ghép cặp lần 2 (Thứ 5 – Thứ 7)",
        "final": "Chung kết BO3 (Chủ Nhật)",
        "done": "Đã xong, chờ nhậm chức",
    }.get(phase, phase)


def leader_term_text(state):
    leader = state["leader"]
    if not leader.get("user_id"):
        text = "Chưa có Gym Leader"
    else:
        try:
            end = parse_dt(leader["term_end"])
            text = f"<@{leader['user_id']}> — hết nhiệm kỳ <t:{int(end.timestamp())}:R>"
        except (KeyError, TypeError, ValueError):
            text = f"<@{leader['user_id']}>"
    elect = state.get("leader_elect")
    if elect and elect != leader.get("user_id"):
        text += f"\n👑 Kế nhiệm: <@{elect}> — nhậm chức 00:00 thứ 2"
    return text


def match_line(match):
    a, b = match["players"]
    return f"<@{a}> vs <@{b}>"


def tournament_summary(state, current):
    tournament = state["tournament"]
    phase = tournament["phase"]
    if phase == "idle":
        return "*Chưa có giải nào đang chạy.*"
    lines = [f"**WL{tournament['wl']}** · {phase_label(phase)}"]
    if tournament["players"]:
        lines.append("**Còn lại:** " + ", ".join(f"<@{uid}>" for uid in tournament["players"]))
    for match in open_matches(state):
        best_of = "BO3" if match["best_of"] == 3 else "BO1"
        lines.append(f"⚔️ `#{match['id']}` {best_of} — {match_line(match)}")
    deadline = tournament_deadline(tournament)
    if deadline:
        lines.append(f"**Hạn chót:** <t:{int(deadline.timestamp())}:R>")
    return "\n".join(lines)


def week_summary(state, current):
    """Thông tin ngắn để hiển thị trong /gym."""
    signup = state["signup"]
    wl = signup.get("wl") or 0
    lines = [f"**Đăng ký:** WL{wl} — {len(signup['registrations'])} người"]
    if signup.get("open"):
        lines.append(f"**Chốt đăng ký:** <t:{int(signup_close_at(current).timestamp())}:R>")
    lines.append("")
    lines.append(tournament_summary(state, current))
    submission = pending_submission(state)
    if submission:
        label = "chờ đối thủ xác nhận" if submission.get("status") == "pending" else "đang tranh chấp"
        lines.append(
            f"**📤 Biên bản:** <@{submission['submitter_id']}> nộp `{submission.get('score')}` — {label}"
        )
    return "\n".join(lines)
