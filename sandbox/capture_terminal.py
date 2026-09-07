#!/usr/bin/env python3
"""Run a command in a pseudo-terminal and save its visible terminal screen as PNG."""

from __future__ import annotations

import argparse
import os
import pty
import re
import select
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MAX_LINES = 42
WIDTH = 1440
PADDING = 28
LINE_HEIGHT = 24


def _font() -> ImageFont.ImageFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, 18)
    return ImageFont.load_default()


def _run(command: list[str]) -> tuple[str, int]:
    master, slave = pty.openpty()
    environment = os.environ | {"TERM": "xterm-256color", "COLUMNS": "150", "LINES": "48"}
    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=environment,
    )
    os.close(slave)
    chunks: list[bytes] = []
    while process.poll() is None:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunks.append(os.read(master, 65_536))
            except OSError:
                break
    while True:
        try:
            ready, _, _ = select.select([master], [], [], 0)
            if not ready:
                break
            chunks.append(os.read(master, 65_536))
        except OSError:
            break
    os.close(master)
    return b"".join(chunks).decode("utf-8", errors="replace"), process.returncode or 0


def _render(output: str, command: list[str], exit_code: int, title: str, destination: Path) -> None:
    clean = ANSI_ESCAPE.sub("", output).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.expandtabs(4)[:145] for line in clean.splitlines() if line.strip()]
    header = [title, "$ " + " ".join(command), f"[exit {exit_code}]"]
    lines = (header + lines)[-MAX_LINES:]
    font = _font()
    height = PADDING * 2 + max(1, len(lines)) * LINE_HEIGHT
    image = Image.new("RGB", (WIDTH, height), "#101418")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        color = "#82d2a2" if index == 0 else "#f0f4f8"
        draw.text((PADDING, PADDING + index * LINE_HEIGHT), line, font=font, fill=color)
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = PngInfo()
    metadata.add_text("ctf-agent-source", "capture-terminal")
    metadata.add_text("ctf-agent-command", " ".join(command))
    metadata.add_text("ctf-agent-exit-code", str(exit_code))
    image.save(destination, "PNG", pnginfo=metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title", default="Terminal evidence")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command after -- is required")
    output, exit_code = _run(command)
    _render(output, command, exit_code, args.title, args.output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
