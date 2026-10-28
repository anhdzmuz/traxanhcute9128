"""Luật chơi và các thao tác trên trạng thái Dark Gym.

Module này cố ý KHÔNG import discord hay psycopg: mọi hàm chỉ nhận dict `data`
(trạng thái đầy đủ) và thời gian, rồi sửa trực tiếp dict đó. Nhờ vậy:

* có thể chạy trong `update_data(...)` (khóa dòng DB) mà không có `await` bên trong;
* có thể unit test mà không cần Discord hay Postgres.
"""

import copy
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

ANNOUNCE_INTERVAL = timedelta(minutes=30)
PROTECTION = timedelta(hours=12)
REVENGE_COOLDOWN = timedelta(hours=48)
STARDUST_TICK = timedelta(hours=3)
STARDUST_PER_TICK = 10
DEFENSE_REWARD = 15
MAX_HISTORY = 100

DEFAULT_DATA = {
    "towers": {
        "1": {"title": "Antumbra Overlord", "user_id": None, "protected_until": None, "defense_streak": 0},
        "2": {"title": "Umbra Weaver", "user_id": None, "protected_until": None, "defense_streak": 0},
        "3": {"title": "Penumbra Sentinel", "user_id": None, "protected_until": None, "defense_streak": 0},
    },
    "wallets": {},
    "cooldowns": {},
    "active_registrations": {},
    "active_matches": {},
    "pending_challenges": {},
    "players": {},
    "match_history": [],
    "stardust_last_tick": None,
}


# --------------------------------------------------------------------------- #
# Thời gian
# --------------------------------------------------------------------------- #

def now():
    return datetime.now(TZ)


def parse_dt(value):
    """ISO string -> datetime có múi giờ Việt Nam. Ném ValueError/TypeError nếu hỏng."""
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def parse_hhmm(value, day):
    parsed = datetime.strptime(value.strip(), "%H:%M")
    return datetime.combine(day, parsed.time(), tzinfo=TZ)


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


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def merge_defaults(data):
    """Bổ sung các trường schema mới mà không làm mất dữ liệu đã lưu."""
    merged = copy.deepcopy(DEFAULT_DATA)
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "towers" and isinstance(value, dict):
                for tier, info in value.items():
                    if tier in merged["towers"] and isinstance(info, dict):
                        merged["towers"][tier].update(info)
            else:
                merged[key] = value

    for tier, info in merged["towers"].items():
        info.setdefault("title", DEFAULT_DATA["towers"][tier]["title"])
        info.setdefault("user_id", None)
        info.setdefault("protected_until", None)
        info.setdefault("defense_streak", 0)

    for key in ("wallets", "cooldowns", "active_registrations", "active_matches", "pending_challenges", "players"):
        if not isinstance(merged.get(key), dict):
            merged[key] = {}
    if not isinstance(merged.get("match_history"), list):
        merged["match_history"] = []
    return merged


def get_player(data, user_id):
    key = str(user_id)
    player = data["players"].setdefault(key, {})
    player.setdefault("wins", 0)
    player.setdefault("losses", 0)
    player.setdefault("captures", 0)
    player.setdefault("defenses", 0)
    player.setdefault("win_streak", 0)
    player.setdefault("best_win_streak", 0)
    player.setdefault("towers_captured", [])
    player.setdefault("achievements", [])
    return player


def award_achievements(data, user_id):
    player = get_player(data, user_id)
    unlocked = []
    checks = [
        ("first_blood", "⚔️ First Blood", player["wins"] >= 1),
        ("conqueror", "👑 Conqueror", player["captures"] >= 1),
        ("guardian", "🛡️ Guardian", player["defenses"] >= 5),
        ("dark_master", "🌑 Dark Master", player["wins"] >= 20),
        ("unstoppable", "🔥 Unstoppable", player["best_win_streak"] >= 5),
        ("eclipse", "🌘 Eclipse", len(set(player["towers_captured"])) >= 3),
    ]
    for code, name, condition in checks:
        if condition and code not in player["achievements"]:
            player["achievements"].append(code)
            unlocked.append(name)
    return unlocked


def get_achievement_names(player):
    names = {
        "first_blood": "⚔️ First Blood",
        "conqueror": "👑 Conqueror",
        "guardian": "🛡️ Guardian",
        "dark_master": "🌑 Dark Master",
        "unstoppable": "🔥 Unstoppable",
        "eclipse": "🌘 Eclipse",
    }
    return [names[x] for x in player.get("achievements", []) if x in names]


def get_teambuilding_text():
    return (
        "⚠️ **LƯU Ý QUY ĐỊNH TEAMBUILDING:**\n"
        "• **Core Hệ Dark:** Cả Quản tháp & Người thách đấu bắt buộc mang tối thiểu **3/6 Pokémon** hệ Dark.\n"
        "• **Hybrid Team Sheet:**\n"
        "  ➔ Chỉ công khai (Move, Nature, Item, Gender) của **4/6 Pokémon** trong đội.\n"
        "  ➔ **2 Pokémon còn lại** sẽ được giấu kín hoàn toàn mọi thông tin trên."
    )


# --------------------------------------------------------------------------- #
# Stardust
# --------------------------------------------------------------------------- #

def apply_stardust(data, current):
    """Cộng +10 Dark Stardust cho mỗi tầng có Quản Tháp, mỗi 3 giờ.

    Mốc thời gian được lưu trong DB nên khởi động lại bot không tạo thưởng mới,
    và thời gian bot tắt vẫn được bù đúng số kỳ đã trôi qua.
    Trả về số tầng được thưởng.
    """
    raw = data.get("stardust_last_tick")
    try:
        last = parse_dt(raw) if raw else None
    except (TypeError, ValueError):
        last = None
    if last is None or last > current:
        data["stardust_last_tick"] = current.isoformat()
        return 0

    periods = int((current - last) // STARDUST_TICK)
    if periods <= 0:
        return 0

    paid = 0
    for info in data["towers"].values():
        user_id = info.get("user_id")
        if user_id:
            key = str(user_id)
            data["wallets"][key] = data["wallets"].get(key, 0) + STARDUST_PER_TICK * periods
            paid += 1
    data["stardust_last_tick"] = (last + STARDUST_TICK * periods).isoformat()
    return paid


# --------------------------------------------------------------------------- #
# Cooldown
# --------------------------------------------------------------------------- #

def cooldown_remaining(data, user_id, tier, current):
    """Thời gian cooldown còn lại của người chơi ở tầng này, hoặc None. Tự dọn mục hết hạn."""
    key = f"{user_id}_{tier}"
    raw = data["cooldowns"].get(key)
    if not raw:
        return None
    try:
        until = parse_dt(raw)
    except (TypeError, ValueError):
        data["cooldowns"].pop(key, None)
        return None
    if until <= current:
        data["cooldowns"].pop(key, None)
        return None
    return until - current


# --------------------------------------------------------------------------- #
# Lịch thách đấu
# --------------------------------------------------------------------------- #

def validate_window(start, end, current):
    """Trả về mã lỗi ('past', 'order', 'range') hoặc None nếu khung giờ hợp lệ."""
    if start <= current:
        return "past"
    if end <= start:
        return "order"
    for moment in (start, end):
        if moment.hour < 7 or (moment.hour == 23 and moment.minute > 0):
            return "range"
    return None


def request_challenge(data, tier, requester_id, current):
    """Owner yêu cầu Quản Tháp nhập lịch (`/mo_thach_dau`)."""
    tower = data["towers"][tier]
    if not tower.get("user_id"):
        return "no_owner", None
    if tier in data["active_registrations"] or tier in data["active_matches"]:
        return "busy", None
    raw = tower.get("protected_until")
    if raw:
        try:
            protection = parse_dt(raw)
        except (TypeError, ValueError):
            protection = None
        if protection and protection > current:
            return "protected", protection - current
    data["pending_challenges"][tier] = {
        "requested_by": requester_id,
        "requested_at": current.isoformat(),
        "sent": True,
    }
    return "ok", {"user_id": tower["user_id"], "title": tower.get("title", "Dark Gym")}


def cancel_request(data, tier):
    data["pending_challenges"].pop(tier, None)


def cancel_challenge(data, tier):
    """`/huy_thach_dau`. Trả về (đã_hủy, id_tin_thông_báo, id_kênh)."""
    session = data["active_registrations"].pop(tier, None)
    pending = data["pending_challenges"].pop(tier, None)
    removed = session is not None or pending is not None
    message_id = session.get("announcement_message_id") if session else None
    channel_id = session.get("channel_id") if session else None
    return removed, message_id, channel_id


def submit_schedule(data, tier, user_id, start, end, current, channel_id=None):
    """Quản Tháp nhập (hoặc sửa) lịch rảnh cho ngày mai."""
    tower = data["towers"][tier]
    if tower.get("user_id") != user_id:
        return "not_owner", None
    if tier in data["active_matches"]:
        return "match", None
    existing = data["active_registrations"].get(tier)
    if existing and existing.get("challengers"):
        return "has_challengers", None
    for other, session in data["active_registrations"].items():
        if other == tier:
            continue  # lịch cũ của chính tầng này sẽ bị thay thế, không tính là trùng
        try:
            other_start, other_end = parse_dt(session["start_at"]), parse_dt(session["end_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < other_end and end > other_start:
            return "overlap", other

    session = {
        "owner_id": user_id,
        "owner_time": f"{start:%d/%m/%Y %H:%M} - {end:%d/%m/%Y %H:%M} giờ Việt Nam",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "next_announcement_at": (current + ANNOUNCE_INTERVAL).isoformat(),
        "channel_id": channel_id,
        "challengers": [],
        "announcement_message_id": None,
    }
    old_message_id = existing.get("announcement_message_id") if existing else None
    data["active_registrations"][tier] = session
    data["pending_challenges"].pop(tier, None)
    return "ok", {
        "session": copy.deepcopy(session),
        "title": tower.get("title", "Dark Gym"),
        "old_message_id": old_message_id,
    }


def mark_announced(data, tier, start_at, current, message_id, reschedule=True):
    """Ghi nhận id tin thông báo (và lùi mốc nhắc kế tiếp) cho đúng phiên đăng ký."""
    session = data["active_registrations"].get(tier)
    if not session or session.get("start_at") != start_at:
        return False
    if reschedule:
        session["next_announcement_at"] = (current + ANNOUNCE_INTERVAL).isoformat()
    if message_id:
        session["announcement_message_id"] = message_id
    return True


def toggle_registration(data, tier, user_id, current):
    """Người chơi bấm nút Đăng ký / Hủy đăng ký."""
    session = data["active_registrations"].get(tier)
    if not session:
        return "closed", None
    if user_id == session.get("owner_id"):
        return "owner", None
    try:
        start = parse_dt(session["start_at"])
    except (KeyError, TypeError, ValueError):
        return "closed", None
    if current >= start:
        return "locked", None

    people = session.setdefault("challengers", [])
    if user_id in people:
        people.remove(user_id)
        status = "left"
    else:
        remaining = cooldown_remaining(data, user_id, tier, current)
        if remaining:
            return "cooldown", remaining
        people.append(user_id)
        status = "joined"
    return status, {
        "count": len(people),
        "session": copy.deepcopy(session),
        "title": data["towers"][tier].get("title", "Dark Gym"),
    }


def next_match_id(data):
    ids = [int(x["match_id"]) for x in data["match_history"] if str(x.get("match_id", "")).isdigit()]
    ids += [int(x["match_id"]) for x in data["active_matches"].values() if str(x.get("match_id", "")).isdigit()]
    return max(ids + [0]) + 1


def resolve_session(data, tier, current, rng=random):
    """Xử lý một phiên đăng ký đã đến giờ: bốc thăm hoặc đóng phiên.

    Trả về None nếu chưa đến giờ / không có phiên, hoặc (trạng_thái, dữ_liệu):
      ("invalid", None)        phiên hỏng, đã xóa
      ("empty", channel_id)    không ai đăng ký
      ("cancelled", channel_id) mất Quản Tháp hoặc tầng đã có trận
      ("match", info)          đã bốc thăm và tạo Active Match
    """
    session = data["active_registrations"].get(tier)
    if not session:
        return None
    try:
        start, end = parse_dt(session["start_at"]), parse_dt(session["end_at"])
    except (KeyError, TypeError, ValueError):
        del data["active_registrations"][tier]
        return "invalid", None
    if current < start:
        return None

    channel_id = session.get("channel_id")
    candidates = list(dict.fromkeys(session.get("challengers", [])))
    owner_id = data["towers"][tier].get("user_id")
    if not candidates:
        del data["active_registrations"][tier]
        return "empty", channel_id
    if not owner_id or tier in data["active_matches"]:
        del data["active_registrations"][tier]
        return "cancelled", channel_id

    selected = rng.choice(candidates)
    match_id = next_match_id(data)
    data["active_matches"][tier] = {
        "match_id": match_id,
        "tower": int(tier),
        "owner_id": int(owner_id),
        "challenger_id": int(selected),
        "created_at": current.isoformat(),
        "owner_time": session.get("owner_time", "Chưa đặt"),
        "challenger_time": f"{start:%d/%m/%Y %H:%M} giờ Việt Nam",
        "scheduled_start": start.isoformat(),
        "scheduled_end": end.isoformat(),
    }
    del data["active_registrations"][tier]
    return "match", {
        "channel_id": channel_id,
        "match_id": match_id,
        "owner_id": int(owner_id),
        "selected": int(selected),
        "count": len(candidates),
        "start": start,
        "end": end,
    }


# --------------------------------------------------------------------------- #
# Quản lý tháp
# --------------------------------------------------------------------------- #

def promote(data, tier, member_id, current):
    if tier in data["active_matches"]:
        return "match", None
    tower = data["towers"][tier]
    old_owner = tower.get("user_id")
    tower["user_id"] = member_id
    tower["protected_until"] = (current + PROTECTION).isoformat()
    tower["defense_streak"] = 0
    # Đổi Quản Tháp thì yêu cầu / đăng ký của người cũ không còn hiệu lực.
    data["pending_challenges"].pop(tier, None)
    data["active_registrations"].pop(tier, None)
    return "ok", {"old_owner": old_owner, "title": tower["title"]}


def clear_protection(data, tier):
    tower = data["towers"][tier]
    if not tower.get("protected_until"):
        return "none", tower["title"]
    tower["protected_until"] = None
    return "ok", tower["title"]


def record_result(data, tier, outcome, current):
    """Chốt kết quả Active Match. outcome: 'thang' (challenger thắng) hoặc 'thua' (Quản Tháp thắng)."""
    match = data["active_matches"].get(tier)
    if not match:
        return "no_match", None

    challenger_id = int(match["challenger_id"])
    owner_id = int(match["owner_id"])
    tower = data["towers"][tier]
    challenger = get_player(data, challenger_id)
    owner = get_player(data, owner_id)

    if outcome == "thang":
        challenger["wins"] += 1
        challenger["captures"] += 1
        challenger["win_streak"] += 1
        challenger["best_win_streak"] = max(challenger["best_win_streak"], challenger["win_streak"])
        if tier not in challenger["towers_captured"]:
            challenger["towers_captured"].append(tier)
        owner["losses"] += 1
        owner["win_streak"] = 0
        tower["user_id"] = challenger_id
        tower["protected_until"] = (current + PROTECTION).isoformat()
        tower["defense_streak"] = 0
    else:
        challenger["losses"] += 1
        challenger["win_streak"] = 0
        owner["wins"] += 1
        owner["defenses"] += 1
        owner["win_streak"] += 1
        owner["best_win_streak"] = max(owner["best_win_streak"], owner["win_streak"])
        tower["defense_streak"] = tower.get("defense_streak", 0) + 1
        data["wallets"][str(owner_id)] = data["wallets"].get(str(owner_id), 0) + DEFENSE_REWARD
        data["cooldowns"][f"{challenger_id}_{tier}"] = (current + REVENGE_COOLDOWN).isoformat()

    achievements = award_achievements(data, challenger_id) + award_achievements(data, owner_id)

    match_id = match["match_id"]
    data["match_history"].append({
        "match_id": match_id,
        "tower": int(tier),
        "challenger_id": challenger_id,
        "owner_id": owner_id,
        "result": outcome,
        "ended_at": current.strftime("%Y-%m-%d %H:%M"),
    })
    data["match_history"] = data["match_history"][-MAX_HISTORY:]
    del data["active_matches"][tier]
    return "ok", {
        "match_id": match_id,
        "challenger_id": challenger_id,
        "owner_id": owner_id,
        "title": tower["title"],
        "achievements": list(dict.fromkeys(achievements)),
    }
