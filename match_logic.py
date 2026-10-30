"""Luật giải đấu hàng tuần của Gym.

Module này cố ý KHÔNG import discord hay psycopg: mọi hàm chỉ nhận dict `state`
(trạng thái đầy đủ) và thời gian, rồi sửa trực tiếp dict đó. Nhờ vậy:

* có thể chạy trong `update_data(...)` (khóa dòng DB) mà không có `await` bên trong;
* có thể unit test mà không cần Discord hay Postgres.

Chu kỳ một tuần (giờ Việt Nam):

    Thứ 2 00:00   mở đăng ký (cả tuần)
    CN   00:00    bốc thăm: 0 người -> gia hạn; 1 người -> thách leader;
                  2+ người -> chọn 2, đánh BO1
    CN   06:00    thông báo lại (im lặng)
    CN   18:00    ping nhắc
    CN   23:59    hạn chót; quá hạn coi như không có kết quả, gia hạn leader
"""

import copy
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

NOTICE_HOUR = 6          # Chủ Nhật
PING_HOUR = 18           # Chủ Nhật
QUALIFIER_BEST_OF = 1    # 2 người bốc thăm đánh BO1
TITLE_BEST_OF = 3        # thách đấu leader đánh BO3
MAX_HISTORY = 200

DEFAULT_STATE = {
    "leader": {"user_id": None, "term_start": None, "term_end": None},
    "week": {
        "week_id": None,
        "phase": "idle",   # idle | open | qualifier | title | done
        "registrations": [],
        "picked": [],
        "message_id": None,
        "qualifier": None,
        "title": None,
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


def draw_at(moment):
    """Chủ Nhật 00:00 — thời điểm bốc thăm."""
    return week_start(moment) + timedelta(days=6)


def notice_at(moment):
    return draw_at(moment) + timedelta(hours=NOTICE_HOUR)


def ping_at(moment):
    return draw_at(moment) + timedelta(hours=PING_HOUR)


def deadline_at(moment):
    """Chủ Nhật 23:59:59 — hạn chót nhận kết quả."""
    return draw_at(moment) + timedelta(days=1) - timedelta(seconds=1)


def next_term(current):
    """Nhiệm kỳ leader: từ thứ 2 tuần sau đến hết Chủ Nhật tuần sau."""
    start = week_start(current) + timedelta(days=7)
    end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return start, end


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


def merge_defaults(data):
    """Bổ sung trường mới, giữ dữ liệu cũ có thể chuyển đổi được."""
    state = copy.deepcopy(DEFAULT_STATE)
    if not isinstance(data, dict):
        return state

    leader = data.get("leader")
    if isinstance(leader, dict):
        state["leader"].update({
            "user_id": leader.get("user_id"),
            "term_start": leader.get("term_start"),
            "term_end": leader.get("term_end"),
        })

    week = data.get("week")
    if isinstance(week, dict):
        state["week"].update({k: week[k] for k in state["week"] if k in week})
        for key in ("registrations", "picked"):
            if not isinstance(state["week"].get(key), list):
                state["week"][key] = []
        if not isinstance(state["week"].get("flags"), dict):
            state["week"]["flags"] = {}
    if state["week"].get("phase") not in ("idle", "open", "qualifier", "title", "done"):
        state["week"]["phase"] = "idle"

    if isinstance(data.get("priority"), dict):
        state["priority"] = {str(k): int(v) for k, v in data["priority"].items() if str(v).lstrip("-").isdigit()}

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
    """Bật/tắt đăng ký trong tuần. Trả về (trạng_thái, dữ_liệu)."""
    week = state["week"]
    if week["phase"] != "open":
        return "closed", None
    if state["leader"].get("user_id") == user_id:
        return "leader", None
    registrations = week["registrations"]
    if user_id in registrations:
        registrations.remove(user_id)
        return "left", {"count": len(registrations)}
    registrations.append(user_id)
    return "joined", {"count": len(registrations)}


def _pick_two(candidates, priority, rng):
    """Ưu tiên người đăng ký nhiều tuần mà chưa được chọn."""
    def score(uid):
        return priority.get(str(uid), 0)

    ordered = sorted(candidates, key=score, reverse=True)
    top_score = score(ordered[0])
    top = [uid for uid in ordered if score(uid) == top_score]
    if len(top) >= 2:
        return rng.sample(top, 2)
    rest = [uid for uid in ordered if uid not in top]
    return [top[0]] + rng.sample(rest, 1)


def _draw(state, current, rng):
    """Chủ Nhật 00:00. Trả về (loại, dữ_liệu)."""
    week = state["week"]
    leader_id = state["leader"].get("user_id")
    registrations = [uid for uid in dict.fromkeys(week["registrations"]) if uid != leader_id]

    if not registrations:
        extend_term(state, current)
        week["phase"] = "done"
        week["picked"] = []
        return "empty", {"leader_id": leader_id}

    if len(registrations) == 1:
        challenger = registrations[0]
        week["picked"] = [challenger]
        state["priority"][str(challenger)] = 0
        if not leader_id:
            _crown(state, challenger, current)
            week["phase"] = "done"
            return "crowned", {"user_id": challenger}
        week["phase"] = "title"
        week["title"] = {
            "challenger_id": challenger,
            "leader_id": leader_id,
            "best_of": TITLE_BEST_OF,
        }
        return "single", {"challenger_id": challenger, "leader_id": leader_id}

    pair = _pick_two(registrations, state["priority"], rng)
    week["picked"] = list(pair)
    week["phase"] = "qualifier"
    week["qualifier"] = {"players": list(pair), "best_of": QUALIFIER_BEST_OF}

    # Ai không được chọn thì tăng ưu tiên cho tuần sau.
    for uid in registrations:
        key = str(uid)
        if uid in pair:
            state["priority"][key] = 0
        else:
            state["priority"][key] = state["priority"].get(key, 0) + 1
    return "pair", {"players": list(pair)}


# --------------------------------------------------------------------------- #
# Kết quả
# --------------------------------------------------------------------------- #

def record_qualifier(state, winner_id, current):
    """Chốt BO1 giữa 2 người được bốc thăm."""
    week = state["week"]
    if week["phase"] != "qualifier":
        return "phase", None
    players = week.get("qualifier", {}).get("players", [])
    if winner_id not in players:
        return "not_in_match", None

    loser_id = next(uid for uid in players if uid != winner_id)
    winner = _bump(state, winner_id, "wins")
    winner["streak"] += 1
    winner["best_streak"] = max(winner["best_streak"], winner["streak"])
    loser = get_player(state, loser_id)
    loser["losses"] += 1
    loser["streak"] = 0

    week["qualifier"]["winner_id"] = winner_id
    week["qualifier"]["at"] = current.isoformat()
    match_id = _push_history(state, {
        "type": "qualifier",
        "week": week["week_id"],
        "winner_id": winner_id,
        "loser_id": loser_id,
        "at": current.isoformat(),
    })

    leader_id = state["leader"].get("user_id")
    if not leader_id:
        _crown(state, winner_id, current)
        week["phase"] = "done"
        return "ok", {"match_id": match_id, "crowned": winner_id}

    week["phase"] = "title"
    week["title"] = {
        "challenger_id": winner_id,
        "leader_id": leader_id,
        "best_of": TITLE_BEST_OF,
    }
    return "ok", {"match_id": match_id, "challenger_id": winner_id, "leader_id": leader_id}


def record_title(state, winner_id, current):
    """Chốt BO3 giữa người thách đấu và leader hiện tại."""
    week = state["week"]
    if week["phase"] != "title":
        return "phase", None
    title = week.get("title") or {}
    challenger = title.get("challenger_id")
    leader_id = title.get("leader_id")
    if winner_id not in (challenger, leader_id):
        return "not_in_match", None

    if winner_id == challenger:
        loser_id = leader_id
        winner = _bump(state, challenger, "wins")
        winner["streak"] += 1
        winner["best_streak"] = max(winner["best_streak"], winner["streak"])
        loser = get_player(state, leader_id)
        loser["losses"] += 1
        loser["streak"] = 0
        _crown(state, challenger, current)
        new_leader = challenger
    else:
        loser_id = challenger
        winner = _bump(state, leader_id, "wins")
        winner["streak"] += 1
        winner["best_streak"] = max(winner["best_streak"], winner["streak"])
        loser = get_player(state, challenger)
        loser["losses"] += 1
        loser["streak"] = 0
        extend_term(state, current)
        new_leader = leader_id

    match_id = _push_history(state, {
        "type": "title",
        "week": week["week_id"],
        "winner_id": winner_id,
        "loser_id": loser_id,
        "at": current.isoformat(),
    })
    week["title"]["winner_id"] = winner_id
    week["title"]["at"] = current.isoformat()
    week["phase"] = "done"
    return "ok", {
        "match_id": match_id,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "leader_id": new_leader,
        "leader_changed": new_leader == challenger,
    }


# --------------------------------------------------------------------------- #
# Vòng lặp tuần
# --------------------------------------------------------------------------- #

def _start_week(state, current):
    state["week"] = {
        "week_id": week_id(current),
        "phase": "open",
        "registrations": [],
        "picked": [],
        "message_id": None,
        "qualifier": None,
        "title": None,
        "flags": {},
    }


def ensure_week(state, current, rng=random):
    """Bước tiến của tuần. Trả về danh sách hành động để cog gửi tin nhắn.

    Idempotent: gọi lại nhiều lần trong cùng một mốc thời gian không tạo
    thông báo trùng, nhờ cờ trong `week["flags"]` và `phase`.
    """
    actions = []
    week = state["week"]
    current_week = week_id(current)

    if week["week_id"] != current_week:
        # Sang tuần mới. Tuần trước kết thúc dang dở (bot tắt, không ai chốt
        # kết quả) thì leader được gia hạn, rồi mở đăng ký tuần này.
        previous_message_id = week.get("message_id")
        if week["phase"] in ("open", "qualifier", "title"):
            extend_term(state, current)
            kind = "missed" if week["phase"] == "open" else "timeout"
            actions.append((kind, {"week_id": week["week_id"]}))
        _start_week(state, current)
        payload = {"week_id": current_week}
        if previous_message_id:
            payload["previous_message_id"] = previous_message_id
        actions.append(("open", payload))
        return actions

    if week["phase"] == "open":
        if current > deadline_at(current):
            # Bot chỉ sống lại vào cuối Chủ Nhật: không bốc thăm muộn.
            extend_term(state, current)
            week["phase"] = "done"
            return [("missed", {"week_id": week["week_id"]})]
        if current >= draw_at(current):
            kind, payload = _draw(state, current, rng)
            actions.append((f"draw_{kind}", payload))

    if week["phase"] in ("qualifier", "title"):
        if not week["flags"].get("notice") and current >= notice_at(current):
            week["flags"]["notice"] = True
            actions.append(("notice", {"week_id": week["week_id"]}))
        if not week["flags"].get("ping") and current >= ping_at(current):
            week["flags"]["ping"] = True
            actions.append(("ping", {"week_id": week["week_id"]}))
        if current > deadline_at(current):
            extend_term(state, current)
            week["phase"] = "done"
            actions.append(("timeout", {"week_id": week["week_id"]}))

    return actions


# --------------------------------------------------------------------------- #
# Tiện ích hiển thị
# --------------------------------------------------------------------------- #

def phase_label(phase):
    return {
        "idle": "Chưa mở",
        "open": "Đang mở đăng ký",
        "qualifier": "Đang đánh BO1 (2 người bốc thăm)",
        "title": "Đang thách đấu Gym Leader (BO3)",
        "done": "Đã kết thúc tuần",
    }.get(phase, phase)


def leader_term_text(state):
    leader = state["leader"]
    if not leader.get("user_id"):
        return "Chưa có Gym Leader"
    try:
        end = parse_dt(leader["term_end"])
    except (KeyError, TypeError, ValueError):
        return f"<@{leader['user_id']}>"
    return f"<@{leader['user_id']}> — hết nhiệm kỳ <t:{int(end.timestamp())}:R>"


def week_summary(state, current):
    """Thông tin ngắn để hiển thị trong /gym."""
    week = state["week"]
    lines = [f"**Tuần:** `{week['week_id'] or '—'}`", f"**Trạng thái:** {phase_label(week['phase'])}"]
    if week["phase"] == "open":
        lines.append(f"**Đã đăng ký:** {len(week['registrations'])}")
        lines.append(f"**Bốc thăm:** <t:{int(draw_at(current).timestamp())}:R>")
    if week["phase"] == "qualifier":
        players = (week.get("qualifier") or {}).get("players", [])
        lines.append("**Cặp BO1:** " + " vs ".join(f"<@{u}>" for u in players))
    if week["phase"] == "title":
        title = week.get("title") or {}
        lines.append(
            f"**BO3:** <@{title.get('challenger_id')}> vs <@{title.get('leader_id')}> (Gym Leader)"
        )
    if week["phase"] in ("qualifier", "title"):
        lines.append(f"**Hạn chót:** <t:{int(deadline_at(current).timestamp())}:R>")
    return "\n".join(lines)
