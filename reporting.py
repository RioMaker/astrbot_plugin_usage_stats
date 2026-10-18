"""Small, paginated text reports; no raw messages are retained or displayed."""

import unicodedata


def clean(value, limit=80):
    return "".join(
        char for char in str(value) if not unicodedata.category(char).startswith("C")
    )[:limit]


def peak_hours(counts):
    peaks = sorted(enumerate(counts), key=lambda item: (-item[1], item[0]))[:3]
    return (
        "、".join(
            f"{hour:02d}:00–{hour + 1:02d}:00（{count:,}次）"
            for hour, count in peaks
            if count
        )
        or "暂无记录"
    )


def personal_summary(name, data):
    lines = [
        f"{clean(name)} 的使用统计",
        "范围：当前平台与机器人，群聊和私聊累计",
        f"指令调用：{data['command_count']:,} 次",
        f"LLM 调用：{data['llm_calls']:,} 次（失败/中断 {data['failed_calls']:,} 次）",
        f"累计已知 Token：{data['total_tokens']:,}",
        f"已知输入 {data['input_tokens']:,} / 输出 {data['output_tokens']:,}",
        f"未返回总用量：{data['missing_usage']:,} 次（未估算）",
        f"指令高频时段：{peak_hours(data['command_hours'])}",
        f"LLM 高频时段：{peak_hours(data['llm_hours'])}",
        "时段为北京时间的累计小时分布。",
    ]
    if data["commands"]:
        lines.append("常用指令：")
        lines.extend(
            f"/{clean(row['name'])} · {row['count']:,} 次"
            for row in data["commands"][:5]
        )
    lines.extend(
        [
            "/使用统计 指令 [页码] 查看每条指令",
            "/使用统计 模型 [页码] 查看模型用量",
            "包含本次统计查询；未接入的调用与安装前历史不在统计内。",
        ]
    )
    return "\n".join(lines)


def command_details(data):
    lines = [f"个人指令明细 · 第 {data['page']}/{data['pages']} 页"]
    for row in data["commands"]:
        lines.append(f"/{clean(row['name'])} · {row['count']:,} 次")
    if not data["commands"]:
        lines.append("暂无指令记录")
    lines.append(f"共 {data['command_types']} 种，/使用统计 指令 [页码] 翻页")
    return "\n".join(lines)


def model_details(data, page):
    rows = data["models"]
    pages = data["model_pages"]
    page = data["model_page"]
    lines = [f"个人模型明细 · 第 {page}/{pages} 页"]
    for row in rows:
        lines.extend(
            [
                f"{clean(row['provider'])} / {clean(row['model'])}",
                f"  {row['calls']:,} 次 · 已知 {row['total_tokens']:,} Token",
                f"  输入 {row['input_tokens']:,} / 输出 {row['output_tokens']:,}"
                f" · 用量缺失 {row['missing_usage']:,} 次"
                f" · 失败 {row['failed_calls']:,} 次",
            ]
        )
    if not rows:
        lines.append("暂无 LLM 记录")
    return "\n".join(lines)


def leaderboard_text(data):
    lines = ["本群指令使用排行（累计）"]
    for row in data["entries"]:
        lines.append(f"{row['rank']}. {clean(row['name'])} · {row['count']:,} 次")
    if not data["entries"]:
        lines.append("暂无指令记录")
    own = data["self"]
    if own:
        lines.append(f"你在本群：第 {own['rank']} 名 · {own['count']:,} 次")
    lines.append("仅按本群指令次数排名，包含本次查询。")
    return "\n".join(lines)
