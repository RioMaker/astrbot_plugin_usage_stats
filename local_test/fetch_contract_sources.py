"""Fetch the four pinned official source files used by optional contract tests."""

import urllib.request
from pathlib import Path

COMMIT = "dffe922773725e9c178392c423676ad65044748c"
FILES = (
    "astrbot/core/pipeline/scheduler.py",
    "astrbot/core/pipeline/context_utils.py",
    "astrbot/core/pipeline/process_stage/method/star_request.py",
    "astrbot/core/star/filter/command.py",
)


def main():
    directory = Path(__file__).resolve().parent / "output" / "contract_sources"
    directory.mkdir(parents=True, exist_ok=True)
    for source in FILES:
        url = f"https://raw.githubusercontent.com/AstrBotDevs/AstrBot/{COMMIT}/{source}"
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read(1024 * 1024)
        (directory / source.replace("/", "__")).write_bytes(content)
    print(directory)


if __name__ == "__main__":
    main()
