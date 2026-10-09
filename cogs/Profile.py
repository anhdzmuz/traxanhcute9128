import io
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

from .core import get_player, get_achievement_names, is_gym_channel, load_data

ROOT = Path(__file__).resolve().parent.parent
BG_PATH = ROOT / "assets" / "profile_bg.jpg"
W, H = 1200, 1200


def _font(size, bold=False):
    candidates = (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"] if bold else
                 ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"])
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _fit_cover(image, size):
    return ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0]-1, size[1]-1), radius=radius, fill=255)
    return mask


def _center_text(draw, xy, text, font, fill, anchor="mm", stroke_width=0, stroke_fill=(0,0,0)):
    draw.text(xy, str(text), font=font, fill=fill, anchor=anchor, stroke_width=stroke_width, stroke_fill=stroke_fill)


def render_profile(display_name, avatar_bytes, player, balance, title, achievements):
    # Background and soft vignette
    canvas = Image.new("RGB", (W, H), (16, 18, 25))
    if BG_PATH.exists():
        bg = _fit_cover(Image.open(BG_PATH), (W, H))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (13, 17, 27)), 0.28)
        canvas.paste(bg, (0, 0))
    else:
        canvas = Image.new("RGB", (W, H), (20, 23, 32))
    draw = ImageDraw.Draw(canvas, "RGBA")
    white = (241, 244, 250, 255)
    muted = (177, 190, 213, 255)
    accent = (145, 170, 218, 255)

    # Dark translucent header/banner
    banner_box = (48, 160, 1152, 555)
    draw.rounded_rectangle(banner_box, radius=42, fill=(8, 10, 16, 190), outline=(220, 228, 245, 220), width=4)
    # Use the provided newspaper/marble image inside banner
    if BG_PATH.exists():
        banner = _fit_cover(Image.open(BG_PATH), (1096, 387))
        banner = Image.blend(banner, Image.new("RGB", banner.size, (4, 5, 8)), 0.18)
        canvas.paste(banner, (52, 164), _rounded_mask(banner.size, 36))
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle(banner_box, radius=42, outline=(220, 228, 245, 225), width=4)
    draw.rounded_rectangle((700, 465, 1135, 548), radius=25, fill=(8, 10, 17, 180))
    draw.text((1095, 507), display_name[:26], font=_font(42, True), fill=white, anchor="rm")

    # Avatar: fetched live from Discord, preserve its original colors, no Discord logo
    avatar_size = 300
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
    except Exception:
        avatar = Image.new("RGB", (avatar_size, avatar_size), (100, 110, 135))
    avatar = ImageOps.fit(avatar, (avatar_size, avatar_size), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (avatar_size, avatar_size), 0)
    ImageDraw.Draw(mask).ellipse((2, 2, avatar_size-3, avatar_size-3), fill=255)
    # soft avatar shadow
    shadow = Image.new("RGBA", (avatar_size+50, avatar_size+50), (0,0,0,0))
    ImageDraw.Draw(shadow).ellipse((20, 20, avatar_size+30, avatar_size+30), fill=(0,0,0,180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))
    canvas.paste(shadow, (W//2-avatar_size//2-25, 400), shadow)
    canvas.paste(avatar, (W//2-avatar_size//2, 410), mask)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.ellipse((W//2-avatar_size//2-5, 405, W//2+avatar_size//2+5, 715), outline=(232, 238, 250, 255), width=8)
    draw.ellipse((W//2-avatar_size//2+5, 415, W//2+avatar_size//2-5, 705), outline=(112, 139, 190, 255), width=3)

    # Main stats card
    card = (70, 720, 1130, 1135)
    draw.rounded_rectangle((card[0]+8, card[1]+12, card[2]+8, card[3]+12), radius=38, fill=(0,0,0,150))
    draw.rounded_rectangle(card, radius=38, fill=(13, 16, 25, 235), outline=(186, 202, 231, 255), width=4)
    draw.rounded_rectangle((82, 732, 1118, 1123), radius=30, outline=(83, 103, 143, 255), width=2)

    # Name + title
    draw.text((125, 770), display_name[:22], font=_font(48, True), fill=white)
    draw.rounded_rectangle((125, 835, 555, 895), radius=20, fill=(36, 43, 61, 255), outline=(133, 157, 202, 255), width=2)
    _center_text(draw, (340, 865), title[:28].upper(), _font(25, True), white)

    # Defense streak column
    draw.line((690, 755, 690, 930), fill=(125, 145, 180, 230), width=2)
    _center_text(draw, (895, 785), "DEFENSE STREAK", _font(25, True), white)
    streak = player.get("defense_streak", player.get("defenses", 0))
    _center_text(draw, (850, 855), str(streak), _font(68, True), white)
    draw.rounded_rectangle((925, 830, 1080, 875), radius=18, outline=(133, 157, 202, 255), width=2)
    _center_text(draw, (1002, 853), f"MAX {max(streak, player.get('best_defense_streak', streak))}", _font(17, True), muted)

    draw.line((110, 945, 1090, 945), fill=(123, 143, 177, 230), width=2)
    # Bottom three columns
    columns = [(280, "✦ STAR DUSK", f"{balance:,}"), (600, "⚔ WIN / LOSE", f"{player.get('wins', 0):,} / {player.get('losses', 0):,}"), (920, "♛ DANH HIỆU", title)]
    for x, label, value in columns:
        _center_text(draw, (x, 990), label, _font(22, True), muted)
        _center_text(draw, (x, 1050), value[:22], _font(32 if x != 920 else 25, True), white)
    draw.line((440, 970, 440, 1090), fill=(95, 111, 142, 220), width=2)
    draw.line((760, 970, 760, 1090), fill=(95, 111, 142, 220), width=2)
    # Subtle star ornaments
    for x, y, r in [(80, 100, 12), (1100, 110, 14), (90, 650, 9), (1110, 665, 10), (610, 700, 13), (45, 1160, 8), (1150, 1160, 8)]:
        draw.line((x-r, y, x+r, y), fill=(202, 216, 240, 210), width=2)
        draw.line((x, y-r, x, y+r), fill=(202, 216, 240, 210), width=2)
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out


class Profile(commands.Cog):
    """Profile dạng ảnh, avatar lấy trực tiếp từ Discord."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="profile", description="Hiển thị profile Dark Gym dạng ảnh.")
    @app_commands.describe(member="Thành viên muốn xem profile (để trống là chính bạn)")
    @is_gym_channel()
    async def dark_profile(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.defer(thinking=True)
        target = member or interaction.user
        data = load_data()
        player = get_player(data, target.id)
        balance = data.get("wallets", {}).get(str(target.id), 0)
        achievements = get_achievement_names(player)
        title = achievements[-1] if achievements else "Dark Trainer"
        try:
            avatar_bytes = await target.display_avatar.replace(size=512, format="png", static_format="png").read()
        except Exception:
            avatar_bytes = await target.display_avatar.read()
        image = render_profile(target.display_name, avatar_bytes, player, balance, title, achievements)
        file = discord.File(image, filename="profile.png")
        await interaction.followup.send(file=file)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
