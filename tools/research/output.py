"""Write research output files and build the structured output_payload."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

_log = logging.getLogger(__name__)
_output_dir: Path | None = None


def init_output_dir(path: str) -> None:
    """Call once at startup. Rejects relative paths; creates the directory."""
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(
            f"research.output_dir must be an absolute path, got {path!r}. "
            "Set an absolute path in config features.research.output_dir."
        )
    p.mkdir(parents=True, exist_ok=True)
    global _output_dir
    _output_dir = p
    _log.info("research output dir ready  path=%s", p)


def build_output_payload(plan_id: int, doc_type: str, paper: str, sources: list[dict]) -> dict:
    """Assemble the structured output_payload and write files to disk."""
    findings = _parse_sections(paper)
    report_path, findings_path = _write_files(plan_id, paper, findings)
    return {
        "plan_id": plan_id,
        "doc_type": doc_type,
        "report_markdown": paper,
        "findings": findings,
        "sources": sources,
        "report_path": report_path,
        "findings_path": findings_path,
    }


def _parse_sections(paper: str) -> list[dict]:
    """Split markdown into [{title, content}] on ## headings, excluding Sources."""
    sections: list[dict] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in paper.splitlines():
        if re.match(r"^## ", line):
            if current_title is not None:
                sections.append({"title": current_title, "content": "\n".join(current_lines).strip()})
            current_title = line[3:].strip()
            current_lines = []
        else:
            if current_title is not None:
                current_lines.append(line)

    if current_title is not None:
        sections.append({"title": current_title, "content": "\n".join(current_lines).strip()})

    return [s for s in sections if s["title"].lower() != "sources"]


def _write_files(plan_id: int, paper: str, findings: list[dict]) -> tuple[str, str]:
    if _output_dir is None:
        raise RuntimeError("research output dir not initialised — call init_output_dir() at startup")

    out = _output_dir / str(plan_id)
    out.mkdir(parents=True, exist_ok=True)

    report_path = out / "report.md"
    findings_path = out / "findings.json"

    report_path.write_text(paper, encoding="utf-8")
    findings_path.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")

    return str(report_path), str(findings_path)
