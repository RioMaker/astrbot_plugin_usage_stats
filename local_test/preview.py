"""Generate a clearly labelled local style preview using simulated records."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from renderer import ChineseFontUnavailable, render_leaderboard  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "local_test/output/leaderboard.png"
    )
    parser.add_argument("--font", type=Path, default=None)
    parser.add_argument("--rows", type=int, choices=range(0, 21), default=8)
    parser.add_argument("--metric", choices=("commands", "tokens"), default="commands")
    args = parser.parse_args()
    names = [
        "晨间小鼓手",
        "今晚再玩一首",
        "咚咚",
        "一个用于检查昵称截断的超长测试昵称以及额外的说明文字",
        "星星",
        "云与海",
        "橘子汽水",
        "夜猫鼓手",
    ]
    counts = [128456, 92600, 81532, 66020, 45628, 28031, 12070, 8231]
    is_tokens = args.metric == "tokens"
    if is_tokens:
        counts = [
            1284567890,
            92600000,
            81532300,
            66020000,
            4562800,
            2803100,
            120700,
            82310,
        ]
    rows = [
        {
            "user": str(10000001 + index),
            "name": names[index] if index < len(names) else f"模拟成员 {index + 1}",
            "count": counts[index]
            if index < len(counts)
            else max(1, 8000 - index * 200),
            "rank": index + 1,
        }
        for index in range(args.rows)
    ]
    try:
        payload = render_leaderboard(
            rows,
            group_label="模拟数据 · 样式预览",
            title="群 Token 排行" if is_tokens else "群使用排行",
            subtitle="本群累计 · 按已知 Token 排名"
            if is_tokens
            else "本群累计指令次数",
            personal={
                "rank": 12 if args.rows else None,
                "count": (53210 if is_tokens else 5321) if args.rows else 0,
            },
            font_path=args.font,
            metric=args.metric,
            started_at="2026-10-18 17:07:23" if is_tokens else None,
            missing_usage=7 if is_tokens else 0,
        )
    except ChineseFontUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(f"模拟数据样式预览：{args.output.resolve()} ({len(payload):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
