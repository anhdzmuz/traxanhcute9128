"""Test các thành phần UI/logic của cog Badge (không cần Discord hay DB)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
import pytest  # noqa: E402

import cogs.Badge as badge  # noqa: E402
import cogs.core as core  # noqa: E402


class FakePermissions:
    def __init__(self, administrator=False):
        self.administrator = administrator


class FakeUser:
    def __init__(self, user_id, administrator=False):
        self.id = user_id
        self.guild_permissions = FakePermissions(administrator)


class FakeMember(FakeUser):
    def __init__(self, user_id, name, administrator=False):
        super().__init__(user_id, administrator)
        self.display_name = name
        self._mention = f"<@{user_id}>"

    @property
    def mention(self):
        return self._mention


HOLDERS = [101, 102, 103, 104, 105]


def test_room_view_custom_ids_and_callbacks():
    view = badge._room_view(42)
    ids = [child.custom_id for child in view.children]
    assert ids == ["badge:done:42", "badge:confirm:42", "badge:cancel:42"]
    # Callback rỗng để view trong bộ nhớ không báo "unknown callback";
    # xử lý thật nằm ở on_interaction.
    assert all(child.callback is not None for child in view.children)
    labels = [child.label for child in view.children]
    assert labels == ["Đã thách đấu council xong", "Xác nhận", "Hủy thách đấu"]


def test_room_view_stage_locks_buttons():
    opening = {c.custom_id: c.disabled for c in badge._room_view(42).children}
    assert opening["badge:done:42"] is False
    assert opening["badge:confirm:42"] is True
    assert opening["badge:cancel:42"] is False

    awaiting = {
        c.custom_id: c.disabled for c in badge._room_view(42, "awaiting").children
    }
    assert awaiting["badge:done:42"] is True
    assert awaiting["badge:confirm:42"] is False
    assert awaiting["badge:cancel:42"] is False


def test_join_view_is_persistent():
    view = badge.BadgeJoinView()
    assert [child.custom_id for child in view.children] == [badge.JOIN_CUSTOM_ID]
    assert view.timeout is None


def test_room_embed_mentions_council_and_stardust():
    challenger = FakeMember(2001, "ThachDau")
    council = [FakeMember(101, "Shady"), FakeMember(102, "ToKoPi")]
    embed = badge._room_embed(challenger, council, "chuyển **50 stardust** cho <@9>")
    assert "SHADOW COUNCIL" in embed.title
    assert challenger.mention in embed.description
    assert council[0].mention in embed.description
    assert council[1].mention in embed.description
    assert "50 stardust" in embed.description
    assert "thắng gym leader mới có huy hiệu" in embed.description.lower()


def test_council_ids_returns_every_configured_member():
    holders = [101, 102, 103, 104]
    assert badge._council_ids(holders) == [101, 102, 103, 104]

    five = [101, 102, 103, 104, 105]
    assert badge._council_ids(five) == [101, 102, 103, 104, 105]


def test_stardust_text_default_and_custom():
    text = badge._stardust_text(50, 123, None)
    assert "50 stardust" in text
    assert "<@123>" in text

    custom = badge._stardust_text(50, 123, "Câu riêng của owner")
    assert custom == "Câu riêng của owner"


def test_stardust_text_without_target():
    text = badge._stardust_text(75, None, None)
    assert "75 stardust" in text
    assert "Gym Leader" in text


def test_stardust_target_defaults_to_owner():
    assert badge._stardust_target(None) == badge.OWNER_ID
    assert badge._stardust_target(123) == 123


def test_reset_view_custom_ids():
    step1 = badge._reset_view(1)
    assert [c.custom_id for c in step1.children] == ["badge:reset:1"]

    step2 = badge._reset_view(2)
    assert [c.custom_id for c in step2.children] == [
        "badge:reset:2",
        "badge:reset:cancel",
    ]


def test_can_judge_allows_council_owner_and_admin():
    owner = FakeUser(badge.OWNER_ID)
    council = FakeUser(102)
    admin = FakeUser(999, administrator=True)
    member = FakeUser(2001)

    assert badge.can_judge(owner, HOLDERS) is True
    assert badge.can_judge(council, HOLDERS) is True
    assert badge.can_judge(admin, HOLDERS) is True
    assert badge.can_judge(member, HOLDERS) is False


def test_can_judge_handles_missing_permissions():
    class DMUser:
        id = 2001

    assert badge.can_judge(DMUser(), []) is False


def test_commands_are_registered_with_expected_names():
    names = sorted(cmd.name for cmd in badge.Badge.__cog_app_commands__)
    assert names == [
        "battle_bage",
        "check_history_bage",
        "dat_council",
        "dat_stardust",
        "panel_huy_hieu",
        "reset_huy_hieu",
        "trao_huy_hieu",
    ]


def test_dat_council_accepts_up_to_ten_members():
    command = next(
        cmd for cmd in badge.Badge.__cog_app_commands__ if cmd.name == "dat_council"
    )
    params = [p.name for p in command.parameters]
    assert params == [f"nguoi_{index}" for index in range(1, 11)]
    for param in command.parameters:
        assert param.type is discord.AppCommandOptionType.user
    # 4 ô đầu bắt buộc, các ô 5-10 có thể để trống.
    for param in command.parameters[:4]:
        assert param.required is True
    for param in command.parameters[4:]:
        assert param.required is False


def test_table_init_short_circuits_when_ready():
    """Sau lần đầu thành công, không được mở kết nối DB nữa."""
    original = badge._tables_ready
    badge._tables_ready = True
    try:
        badge._init_tables()  # không được ném lỗi vì không chạm DB
    finally:
        badge._tables_ready = original


def test_queue_limit_is_two():
    assert badge.MAX_QUEUE == 2


class FakeChannelClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _channel_id):
        return self.channel

    async def fetch_channel(self, _channel_id):
        return None


class FakeGuild:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _channel_id):
        return self.channel


def make_thread(parent):
    thread = object.__new__(discord.Thread)
    thread.guild = FakeGuild(parent)
    thread.parent_id = 1
    return thread


def test_thread_parent_follows_thread_to_its_parent():
    text = object.__new__(discord.TextChannel)
    forum = object.__new__(discord.ForumChannel)
    thread = make_thread(text)

    assert core.thread_parent(thread) is text
    assert core.thread_parent(text) is text
    assert core.thread_parent(forum) is forum
    assert core.thread_parent(None) is None


def test_resolve_thread_parent_accepts_text_thread_and_forum():
    text = object.__new__(discord.TextChannel)
    forum = object.__new__(discord.ForumChannel)
    thread = make_thread(text)

    parent, kind = asyncio.run(core.resolve_thread_parent(FakeChannelClient(text), 1))
    assert parent is text
    assert kind == "text"

    parent, kind = asyncio.run(core.resolve_thread_parent(FakeChannelClient(thread), 1))
    assert parent is text
    assert kind == "text"

    parent, kind = asyncio.run(core.resolve_thread_parent(FakeChannelClient(forum), 1))
    assert parent is forum
    assert kind == "forum"


def test_resolve_thread_parent_reports_missing_channel():
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(core.resolve_thread_parent(FakeChannelClient(None), 1565716218084855959))

    message = str(excinfo.value)
    assert "1565716218084855959" in message
    assert "không tìm thấy kênh" in message
    assert "MATCH_CHANNEL_ID" in message


class FakeParentClient:
    def __init__(self, channels):
        self.channels = channels

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels.get(channel_id)


def test_resolve_thread_parent_fetches_uncached_parent():
    text = object.__new__(discord.TextChannel)
    thread = object.__new__(discord.Thread)
    thread.guild = FakeGuild(None)
    thread.parent_id = 777

    parent, kind = asyncio.run(
        core.resolve_thread_parent(FakeParentClient({1: thread, 777: text}), 1)
    )

    assert parent is text
    assert kind == "text"


class FakeRoomMember(FakeMember):
    def __init__(self, user_id, name):
        super().__init__(user_id, name)


class FakeRoomThread:
    id = 999
    mention = "<#999>"

    def __init__(self):
        self.added = []
        self.sent = []

    async def add_user(self, member):
        self.added.append(member)

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeRoomGuild:
    id = 1

    def __init__(self, members):
        self.members = members

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        return self.members.get(user_id)


class FakeTextChannel(discord.TextChannel):
    def __init__(self):
        self.created = {}

    async def create_thread(self, **kwargs):
        self.created.update(kwargs)
        return FakeRoomThread()


def test_open_room_creates_private_thread_under_parent_channel(monkeypatch):
    text = FakeTextChannel()
    thread_channel = make_thread(text)
    client = FakeParentClient({1: thread_channel})

    challenger = FakeRoomMember(500, "Challenger")
    council = [FakeRoomMember(101 + i, f"Council {i}") for i in range(4)]
    owner = FakeRoomMember(badge.OWNER_ID, "Owner")
    guild = FakeRoomGuild({m.id: m for m in [challenger, *council, owner]})

    saved = []
    monkeypatch.setattr(badge, "MATCH_CHANNEL_ID", 1)
    monkeypatch.setattr(badge, "_get_stardust_sync", lambda _guild_id: (50, None, None))
    monkeypatch.setattr(
        badge, "_set_thread_sync", lambda room_id, thread_id: saved.append((room_id, thread_id))
    )

    thread = asyncio.run(
        badge.open_room(client, guild, 42, challenger.id, [m.id for m in council])
    )

    assert text.created["type"] is discord.ChannelType.private_thread
    assert text.created["invitable"] is False
    assert [m.id for m in thread.added] == [500, 101, 102, 103, 104, badge.OWNER_ID]
    assert thread.sent and thread.sent[0]["embed"].title.startswith("🏅")
    assert saved == [(42, thread.id)]
