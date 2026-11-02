"""Test các thành phần UI/logic của cog Badge (không cần Discord hay DB)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

import cogs.Badge as badge  # noqa: E402


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
    assert ids == ["badge:done:42", "badge:cancel:42"]
    # Callback rỗng để view trong bộ nhớ không báo "unknown callback";
    # xử lý thật nằm ở on_interaction.
    assert all(child.callback is not None for child in view.children)
    labels = [child.label for child in view.children]
    assert labels == ["Đã đánh xong", "Hủy thách đấu"]


def test_join_view_is_persistent():
    view = badge.BadgeJoinView()
    assert [child.custom_id for child in view.children] == [badge.JOIN_CUSTOM_ID]
    assert view.timeout is None


def test_room_embed_uses_holder_name_and_stardust():
    challenger = FakeMember(2001, "ThachDau")
    holder = FakeMember(101, "Shady")
    embed = badge._room_embed(challenger, holder, "chuyển **50 stardust** cho <@9>")
    assert "Shady" in embed.title
    assert challenger.mention in embed.description
    assert holder.mention in embed.description
    assert "50 stardust" in embed.description
    assert "Huy hiệu 1" not in embed.description


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
    ]


def test_dat_council_last_holder_is_optional():
    command = next(
        cmd for cmd in badge.Badge.__cog_app_commands__ if cmd.name == "dat_council"
    )
    params = [p.name for p in command.parameters]
    assert params == ["nguoi_1", "nguoi_2", "nguoi_3", "nguoi_4", "nguoi_5"]
    for param in command.parameters:
        assert param.type is discord.AppCommandOptionType.user
    assert command.parameters[-1].required is False
    for param in command.parameters[:-1]:
        assert param.required is True


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
