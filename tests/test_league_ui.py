"""Test các thành phần UI của cog League (không cần kết nối Discord)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.League as league  # noqa: E402
import match_logic as ml  # noqa: E402

PLAYER_A = 2001
PLAYER_B = 2002


def match_state(best_of=1, phase="qualifier"):
    state = ml.merge_defaults({})
    state["tournament"].update({
        "wl": 3,
        "week_id": "2026-W46",
        "phase": phase,
        "start": ml.now().isoformat(),
        "players": [PLAYER_A, PLAYER_B],
    })
    match = {
        "id": 7,
        "phase": phase,
        "round": 1,
        "players": [PLAYER_A, PLAYER_B],
        "best_of": best_of,
        "status": "open",
        "thread_id": None,
        "submissions": {},
        "created_at": ml.now().isoformat(),
        "ended_at": None,
        "winner_id": None,
        "loser_id": None,
    }
    state["tournament"]["matches"]["7"] = match
    return state, match


def test_submit_view_encodes_match_id():
    view = league._submit_view(7)
    assert [child.custom_id for child in view.children] == ["league:submit:7"]


def test_confirm_view_encodes_match_and_submitter():
    view = league._confirm_view(7, 42)
    assert [child.custom_id for child in view.children] == [
        "league:confirm:7:42",
        "league:dispute:7:42",
    ]


def test_thread_panel_shows_players_and_rules():
    state, match = match_state(1, "qualifier")
    embed = league._thread_panel(state, match)
    assert "BO1" in embed.title
    assert f"<@{PLAYER_A}>" in embed.description
    assert "1-0" in embed.description
    assert any("Hạn chót" in field.name for field in embed.fields)

    state, match = match_state(3, "final")
    embed = league._thread_panel(state, match)
    assert "BO3" in embed.title
    assert "2-1" in embed.description


def test_score_modal_fields():
    modal = league.ScoreModal(3, 7)
    assert modal.match_id == 7
    assert modal.best_of == 3
    assert len(modal.children) == 2
    assert "BO3" in modal.children[0].label

    modal = league.ScoreModal(1, 9)
    assert "BO1" in modal.children[0].label


def test_registration_panel_title_uses_wl_number():
    state = ml.merge_defaults({})
    state["signup"].update({"wl": 7, "week_id": "2026-W45", "open": True})

    embed = league._registration_embed(state, ml.now())

    assert embed.title == "Đăng kí thách đấu Gym Leader - WL7"
    assert "00:00 Thứ 2" in embed.description
    assert "3/6" in str([field.value for field in embed.fields])


def test_rules_embeds_contain_gym_rules():
    embeds = league.rules_embeds()
    text = " ".join(
        [embed.description or "" for embed in embeds]
        + [field.value for embed in embeds for field in embed.fields]
    )
    assert "3/6" in text
    assert "2/6" in text
    assert "BO1" in text
    assert "BO3" in text
    assert "00:00 Thứ 5" in text
    assert "Shiny" in text
    assert "miễn phí" in text


def test_draw_embed_marks_priority_players():
    payload = {"wl": 4, "players": [PLAYER_A, PLAYER_B], "pool": [PLAYER_A, PLAYER_B, 2003], "match_id": 9}
    embed = league._draw_embed("draw_final", payload)
    assert f"<@{PLAYER_A}>" in embed.description
    assert "2003" in str([field.value for field in embed.fields])


def test_unregister_and_reset_views():
    assert [c.custom_id for c in league._unregister_view().children] == ["league:unregister"]
    assert [c.custom_id for c in league._reset_view(1).children] == ["league:reset:1"]
    assert [c.custom_id for c in league._reset_view(2).children] == [
        "league:reset:2",
        "league:reset:cancel",
    ]


class FakeAttachment:
    def __init__(self, filename="a.png", content_type="image/png"):
        self.filename = filename
        self.content_type = content_type


class FakeAuthor:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    def __init__(self, user_id, attachments, message_id=1):
        self.author = FakeAuthor(user_id)
        self.attachments = attachments
        self.id = message_id
        self.jump_url = f"https://discord.com/channels/1/2/{message_id}"


class FakeChannel:
    def __init__(self, messages):
        self.messages = messages
        self.id = 777

    def history(self, limit=60):
        async def iterator():
            # Discord trả tin mới nhất trước.
            for message in reversed(self.messages):
                yield message

        return iterator()


def test_find_proof_picks_latest_image_of_player():
    channel = FakeChannel(
        [
            FakeMessage(PLAYER_A, [FakeAttachment("cu.png")], 1),
            FakeMessage(PLAYER_B, [FakeAttachment("nguoi-khac.png")], 2),
            FakeMessage(PLAYER_A, [], 3),
            FakeMessage(PLAYER_A, [FakeAttachment("moi.png")], 4),
        ]
    )
    proof = asyncio.run(league.find_proof(channel, PLAYER_A))
    assert proof["message_id"] == 4
    assert proof["filename"] == "moi.png"
    assert proof["channel_id"] == 777


def test_find_proof_ignores_text_only_and_non_images():
    channel = FakeChannel(
        [
            FakeMessage(PLAYER_A, [FakeAttachment("log.txt", "text/plain")], 1),
            FakeMessage(PLAYER_A, [], 2),
        ]
    )
    assert asyncio.run(league.find_proof(channel, PLAYER_A)) is None


class FakePerms:
    def __init__(self, **flags):
        self._flags = flags

    def __getattr__(self, name):
        return self._flags.get(name, False)


class FakeGuild:
    def __init__(self, me):
        self.me = me


class FakePermChannel:
    def __init__(self, **flags):
        self.guild = FakeGuild(object())
        self._flags = flags

    def permissions_for(self, member):
        return FakePerms(**self._flags)


def test_missing_permissions_lists_only_missing_ones():
    ok = FakePermChannel(
        send_messages=True, embed_links=True, attach_files=True, read_message_history=True
    )
    assert league.missing_permissions(ok) == []

    bad = FakePermChannel(send_messages=True, embed_links=False, attach_files=True)
    assert league.missing_permissions(bad) == ["Embed Links", "Read Message History"]

    no_guild = FakeChannel([])
    assert league.missing_permissions(no_guild) == []
