from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import renderer


@pytest.fixture
def cjk_font() -> Path:
    try:
        return renderer.resolve_font_path()
    except renderer.ChineseFontUnavailable:
        pytest.skip("rendering tests require an installed CJK font")


def test_empty_leaderboard_is_readable_png(cjk_font: Path) -> None:
    payload = renderer.render_leaderboard([], group_label="测试群", font_path=cjk_font)
    with Image.open(io.BytesIO(payload)) as image:
        assert image.format == "PNG"
        assert image.size == (960, 416)
        assert image.getpixel((5, 5)) == (17, 27, 45)
        assert image.getpixel((40, 240)) == (255, 255, 255)


def test_twenty_rows_and_personal_totals_are_bounded(cjk_font: Path) -> None:
    rows = [
        {"user": str(index + 10000), "name": f"用户 {index}", "count": 200 - index}
        for index in range(20)
    ]
    payload = renderer.render_leaderboard(
        rows,
        group_label="测试群",
        personal={"rank": 121, "count": 9},
        font_path=cjk_font,
    )
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (960, 2530)
    assert len(payload) < 512 * 1024


@pytest.mark.parametrize("metric", ["commands", "tokens"])
def test_long_names_and_large_counts_do_not_overlap(
    cjk_font: Path, monkeypatch, metric: str
) -> None:
    recorded = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, position, text, *args, **kwargs):
        if position[1] in (202, 223):
            box = draw.textbbox(
                position, text, font=kwargs["font"], anchor=kwargs.get("anchor")
            )
            recorded.append((text, box))
        return original(draw, position, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    renderer.render_leaderboard(
        [{"user": "12345", "name": "长昵称测试" * 100, "count": 2**63 - 1}],
        group_label="群名" * 100,
        font_path=cjk_font,
        metric=metric,
    )
    name_text, name_box = next(item for item in recorded if item[0].endswith("…"))
    number_text, number_box = next(
        item for item in recorded if item[0].startswith("9,")
    )
    assert len(name_text) < 50
    assert number_text == "9,223,372,036,854,775,807"
    assert name_box[2] + 20 <= number_box[0]
    assert number_box[2] <= 900


def test_renderer_never_creates_files(
    cjk_font: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    renderer.render_leaderboard(
        [{"user": "1", "name": "甲", "count": 42}],
        group_label="测试群",
        font_path=cjk_font,
    )
    assert list(tmp_path.iterdir()) == []


def test_missing_font_has_actionable_fallback_error(monkeypatch) -> None:
    monkeypatch.setattr(renderer, "_font_candidates", lambda _: [])
    with pytest.raises(
        renderer.ChineseFontUnavailable, match="Noto Sans CJK / WenQuanYi"
    ):
        renderer.render_leaderboard([], group_label="测试群")


def test_invalid_configured_font_does_not_silently_use_another(tmp_path: Path) -> None:
    with pytest.raises(renderer.ChineseFontUnavailable, match="font_path"):
        renderer.resolve_font_path(tmp_path / "missing.ttf")


def test_row_limit_is_enforced_before_font_lookup() -> None:
    with pytest.raises(ValueError, match="最多绘制 20"):
        renderer.render_leaderboard([{}] * 21, group_label="测试群")


@pytest.mark.parametrize("empty", [False, True])
def test_token_leaderboard_shows_units_coverage_and_full_start_time(
    cjk_font: Path, monkeypatch, empty: bool
) -> None:
    recorded = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, position, text, *args, **kwargs):
        box = draw.textbbox(
            position, text, font=kwargs["font"], anchor=kwargs.get("anchor")
        )
        recorded.append((text, box))
        return original(draw, position, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    payload = renderer.render_leaderboard(
        []
        if empty
        else [{"user": "12345", "name": "长昵称" * 100, "count": 2**63 - 1}],
        group_label="测试群",
        title="群 Token 排行",
        subtitle="本群累计 · 按已知 Token 排名",
        personal={"rank": None if empty else 1, "count": 0 if empty else 2**63 - 1},
        metric="tokens",
        started_at="2026-10-18 17:07:23",
        missing_usage=1234,
        font_path=cjk_font,
    )
    texts = [text for text, _ in recorded]
    assert "群 Token 排行" in texts
    assert "统计开始：2026-10-18 17:07:23（北京时间）" in texts
    assert (
        f"已知 Token 含输入与输出 · 当前显示 {0 if empty else 1} 人"
        " · 缺失 1,234 次不估算"
    ) in texts
    assert not any("指令" in text for text in texts)
    if empty:
        assert "本群暂无已知 Token 记录" in texts
        assert "我的本群排名：暂未上榜     累计 Token：0" in texts
    else:
        assert "Token" in texts
        assert (
            "我的本群排名：第 1 名     累计 Token：9,223,372,036,854,775,807" in texts
        )
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (960, 504 if empty else 436)
        for text, box in recorded:
            if text.startswith(("统计开始：", "已知 Token", "我的本群排名：")):
                assert 0 <= box[0] < box[2] < image.width
                assert 0 <= box[1] < box[3] < image.height


def test_unsupported_metric_fails_before_font_lookup() -> None:
    with pytest.raises(ValueError, match="排行指标"):
        renderer.render_leaderboard([], group_label="测试群", metric="unknown")


@pytest.mark.parametrize("count", [-1, 1.5, True, "oops"])
def test_invalid_counters_fail_clearly(cjk_font: Path, count) -> None:
    with pytest.raises(ValueError, match="非负整数"):
        renderer.render_leaderboard(
            [{"count": count}], group_label="测试群", font_path=cjk_font
        )
