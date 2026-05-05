"""Disk write helpers for teaching output files."""
from __future__ import annotations

import json
import logging
from pathlib import Path

_log = logging.getLogger("teaching.output")
_output_dir: Path | None = None


def init_output_dir(path: str) -> None:
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(f"teaching.output_dir must be absolute, got {path!r}")
    p.mkdir(parents=True, exist_ok=True)
    global _output_dir
    _output_dir = p
    _log.info("teaching output dir ready  path=%s", p)


def write_lesson_files(task_id: int, lesson_markdown: str, anki_cards: str) -> tuple[str, str]:
    if _output_dir is None:
        return "", ""
    out = _output_dir / str(task_id)
    out.mkdir(parents=True, exist_ok=True)
    lesson_path = out / "lesson.md"
    cards_path = out / "cards.txt"
    lesson_path.write_text(lesson_markdown, encoding="utf-8")
    cards_path.write_text(anki_cards, encoding="utf-8")
    return str(lesson_path), str(cards_path)


def write_checks_file(task_id: int, checks: list[dict]) -> str:
    if _output_dir is None:
        return ""
    out = _output_dir / str(task_id)
    out.mkdir(parents=True, exist_ok=True)
    checks_path = out / "checks.json"
    checks_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(checks_path)
