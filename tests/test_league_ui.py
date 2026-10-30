"""Test các thành phần UI của chat riêng (không cần kết nối Discord)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.League as league  # noqa: E402
import match_logic as ml  # noqa: E402

PLAYER_A = 2001
PLAYER_B = 2002


def match_state(phase="qualifier"):
    state = ml.merge_defaults({})
    state["week"]["week_id"] = "2026-W45"
    state["week"]["phase"] = phase
    if phase == "qualifier":
        state["week"]["qualifier"] = {"players": [PLAYER_A, PLAYER_B], "best_of": 1}
    else:
        state["week"]["title"] = {
            "challenger_id": PLAYER_A,
            "leader_id": PLAYER_B,
            "best_of": 3,
        }
    return state


def test_submit_view_uses_static_custom_id():
    view = league._submit_view()
    assert [child.custom_id for child in view.children] == ["league:submit"]


def test_confirm_view_encodes_phase_and_submitter():
    view = league._confirm_view("title", 42)
    assert [child.custom_id for child in view.children] == [
        "league:confirm:title:42",
        "league:dispute:title:42",
    ]


def test_thread_panel_shows_players_and_rules():
    embed = league._thread_panel(match_state("qualifier"), "qualifier")
    assert "BO1" in embed.title
    assert f"<@{PLAYER_A}>" in embed.description
    assert "1-0" in embed.description

    embed = league._thread_panel(match_state("title"), "title")
    assert "BO3" in embed.title
    assert "2-1" in embed.description


def test_score_modal_fields():
    modal = league.ScoreModal("title")
    assert modal.phase == "title"
    assert len(modal.children) == 2
    assert "BO3" in modal.children[0].label


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
