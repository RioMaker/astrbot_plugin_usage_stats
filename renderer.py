"""Render compact usage leaderboards without network requests or image caches.

The visual hierarchy references the existing RP leaderboard. All shapes are
drawn here and no RP artwork, font, or rendering implementation is distributed.
"""

from __future__ import annotations

import hashlib
import io
import os
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

WIDTH = 960
MAX_ROWS = 20
HEADER_HEIGHT = 176
ROW_HEIGHT = 112


class ChineseFontUnavailable(RuntimeError):
    """No readable CJK font was found; callers should send a text leaderboard."""


def _font_candidates(font_path: str | Path | None) -> list[Path]:
    if font_path:
        return [Path(font_path).expanduser()]
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return [
        windows_fonts / "msyh.ttc",
        windows_fonts / "msyhbd.ttc",
        windows_fonts / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    ]


def resolve_font_path(font_path: str | Path | None = None) -> Path:
    """Choose a CJK font, rejecting missing-glyph-only fonts explicitly."""
    for candidate in _font_candidates(font_path):
        if not candidate.is_file():
            continue
        try:
            font = ImageFont.truetype(str(candidate), 24)
            first, second = font.getmask("中"), font.getmask("统")
            if bytes(first) and (first.size, bytes(first)) != (
                second.size,
                bytes(second),
            ):
                return candidate
        except (OSError, ValueError):
            continue
    configured = f"（已配置：{font_path}）" if font_path else ""
    raise ChineseFontUnavailable(
        f"未找到可用的中文字体{configured}。请配置 font_path，或安装 "
        "Noto Sans CJK / WenQuanYi 中文字体；本次排行榜可降级为文字。"
    )


class _Fonts:
    def __init__(self, path: Path):
        self.path = path
        self.cache: dict[int, ImageFont.FreeTypeFont] = {}

    def __call__(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self.cache:
            self.cache[size] = ImageFont.truetype(str(self.path), size)
        return self.cache[size]


def _clean_text(value: Any, fallback: str = "") -> str:
    # Names may contain line breaks, bidi controls, or very long content.
    text = "".join(
        char
        for char in str(value or "")[:512]
        if not unicodedata.category(char).startswith("C")
    )
    return " ".join(text.split()) or fallback


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if draw.textlength(text[:middle] + "…", font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…"


def _counter(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("调用次数必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("调用次数必须是非负整数") from exc
    if number < 0 or (not isinstance(value, (int, str)) and number != value):
        raise ValueError("调用次数必须是非负整数")
    return number


def _number_font(
    draw: ImageDraw.ImageDraw, text: str, fonts: _Fonts, width: int
) -> ImageFont.FreeTypeFont:
    for size in range(38, 15, -1):
        font = fonts(size)
        if draw.textlength(text, font=font) <= width:
            return font
    raise ValueError("调用次数超出图片可显示范围，请使用文字排行榜")


def _avatar_color(user: str) -> str:
    palette = ("#497C9A", "#687FB4", "#8C71A8", "#A37A57", "#56958A", "#9B7183")
    return palette[hashlib.blake2s(user.encode("utf-8"), digest_size=1).digest()[0] % 6]


def _draw_row(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    row: Mapping[str, Any],
    index: int,
    unit: str = "次指令调用",
) -> None:
    top = HEADER_HEIGHT + index * ROW_HEIGHT + 8
    rank = _counter(row.get("rank", index + 1))
    if rank < 1:
        raise ValueError("排名必须是正整数")
    count = _counter(row.get("count", 0))
    user = _clean_text(row.get("user"))
    name = _clean_text(row.get("name"), user or "未知用户")
    tones = {
        1: ("#F28B31", "#FFF8EF"),
        2: ("#D8AB28", "#FFFCED"),
        3: ("#94A3B8", "#F8FAFC"),
    }
    accent, fill = tones.get(rank, ("#DDE5EF", "#FFFFFF"))
    draw.rounded_rectangle(
        (32, top, WIDTH - 32, top + 96),
        radius=22,
        fill=fill,
        outline="#E3E9F1",
    )
    draw.ellipse((50, top + 27, 94, top + 71), fill=accent)
    rank_text = str(rank)
    rank_font = fonts(24 if len(rank_text) <= 2 else 16)
    draw.text(
        (72, top + 49),
        rank_text,
        font=rank_font,
        anchor="mm",
        fill="#FFFFFF" if rank <= 3 else "#586A80",
    )

    # Supersampling keeps small avatar edges smooth without saving an asset.
    avatar = Image.new("RGBA", (68 * 3, 68 * 3))
    avatar_draw = ImageDraw.Draw(avatar)
    avatar_draw.ellipse((0, 0, 203, 203), fill=_avatar_color(user or name))
    avatar_draw.text((102, 99), name[0], font=fonts(31 * 3), anchor="mm", fill="white")
    avatar = avatar.resize((68, 68), Image.Resampling.LANCZOS)
    draw.ellipse((113, top + 11, 187, top + 85), fill=accent)
    canvas.paste(avatar, (116, top + 14), avatar)

    number = f"{count:,}"
    number_font = _number_font(draw, number, fonts, 310)
    number_width = draw.textlength(number, font=number_font)
    number_left = WIDTH - 62 - number_width
    name_width = min(470, int(number_left - 212 - 28))
    name_text = _ellipsize(draw, name, fonts(27), name_width)
    draw.text((212, top + 18), name_text, font=fonts(27), fill="#1D2C43")
    detail = f"UID {user}" if user else "群成员"
    draw.text(
        (212, top + 59),
        _ellipsize(draw, detail, fonts(17), 480),
        font=fonts(17),
        fill="#7A899C",
    )
    draw.text(
        (WIDTH - 62, top + 39),
        number,
        font=number_font,
        anchor="rm",
        fill="#BF732F" if rank == 1 else "#455F86",
    )
    draw.text(
        (WIDTH - 62, top + 72),
        unit,
        font=fonts(15),
        anchor="rm",
        fill="#8B99AB",
    )


def render_leaderboard(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_label: str,
    personal: Mapping[str, Any] | None = None,
    title: str = "群使用排行",
    subtitle: str = "本群累计 · 按指令调用次数排行",
    font_path: str | Path | None = None,
    metric: str = "commands",
    started_at: str | None = None,
    missing_usage: int = 0,
) -> bytes:
    """Return a PNG for zero to twenty already-sorted leaderboard rows.

    Each row accepts ``user``, ``name``, ``count``, and optional ``rank``.
    ``personal`` accepts ``rank`` and ``count`` for the requesting member's
    group totals. Token mode uses known input + output totals and displays the
    supplied Beijing start time without truncation. This function never changes
    ranking order or writes files.
    """
    if len(rows) > MAX_ROWS:
        raise ValueError(f"排行榜最多绘制 {MAX_ROWS} 人")
    if metric not in ("commands", "tokens"):
        raise ValueError("排行指标必须为 commands 或 tokens")
    is_tokens = metric == "tokens"
    if is_tokens:
        missing_usage = _counter(missing_usage)
    fonts = _Fonts(resolve_font_path(font_path))
    content_height = len(rows) * ROW_HEIGHT if rows else 180
    footer_height = 114 if personal is not None else 60
    if is_tokens:
        footer_height += 34
    height = HEADER_HEIGHT + content_height + footer_height
    canvas = Image.new("RGB", (WIDTH, height), "#F2F5FA")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 149), fill="#111B2D")
    draw.ellipse((WIDTH - 238, -132, WIDTH + 58, 166), fill="#2457D6")
    draw.ellipse((WIDTH - 111, -61, WIDTH + 100, 159), fill="#7955DE")
    draw.text(
        (44, 28),
        _ellipsize(draw, _clean_text(title, "群使用排行"), fonts(42), 655),
        font=fonts(42),
        fill="#FFFFFF",
    )
    group_text = _clean_text(group_label, "当前群")
    subtitle_text = _clean_text(subtitle)
    summary = f"{group_text} · {subtitle_text}" if subtitle_text else group_text
    draw.text(
        (47, 98),
        _ellipsize(draw, summary, fonts(19), 850),
        font=fonts(19),
        fill="#D2DCEC",
    )
    if rows:
        for index, row in enumerate(rows):
            _draw_row(
                canvas, draw, fonts, row, index, "Token" if is_tokens else "次指令调用"
            )
    else:
        draw.rounded_rectangle(
            (32, HEADER_HEIGHT + 8, WIDTH - 32, HEADER_HEIGHT + 164),
            radius=22,
            fill="#FFFFFF",
            outline="#E3E9F1",
        )
        draw.text(
            (WIDTH // 2, HEADER_HEIGHT + 67),
            "本群暂无已知 Token 记录" if is_tokens else "本群暂无指令使用记录",
            font=fonts(29),
            anchor="mm",
            fill="#455F86",
        )
        draw.text(
            (WIDTH // 2, HEADER_HEIGHT + 113),
            "统计从插件启用后开始累计",
            font=fonts(19),
            anchor="mm",
            fill="#7A899C",
        )

    footer_top = HEADER_HEIGHT + content_height
    if personal is not None:
        count = _counter(personal.get("count", 0))
        rank = personal.get("rank")
        rank_text = f"第 {_counter(rank):,} 名" if rank else "暂未上榜"
        total_text = (
            f"累计 Token：{count:,}" if is_tokens else f"累计指令：{count:,} 次"
        )
        text = f"我的本群排名：{rank_text}     {total_text}"
        draw.rounded_rectangle(
            (32, footer_top + 8, WIDTH - 32, footer_top + 62),
            radius=15,
            fill="#E6EDF8",
        )
        draw.text(
            (51, footer_top + 23),
            _ellipsize(draw, text, fonts(20), 855),
            font=fonts(20),
            fill="#465F83",
        )
    if is_tokens:
        footer = (
            f"已知 Token 含输入与输出 · 当前显示 {len(rows)} 人"
            f" · 缺失 {missing_usage:,} 次不估算"
        )
        start_text = f"统计开始：{_clean_text(started_at, '暂无记录')}（北京时间）"
        draw.text((44, height - 74), footer, font=fonts(16), fill="#7A899C")
        draw.text((44, height - 40), start_text, font=fonts(16), fill="#7A899C")
    else:
        footer = f"仅排行本群指令调用 · 当前显示 {len(rows)} 人 · 不含 LLM 调用次数"
        draw.text((44, height - 40), footer, font=fonts(16), fill="#7A899C")
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
