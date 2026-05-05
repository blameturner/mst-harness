"""Push NocoDB virtual-FS files to a Gitea branch."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.gitea_client import GiteaClient
    from infra.nocodb_client import NocodbClient

_log = logging.getLogger(__name__)

_MAX_FILE_BYTES = 500_000


def push_files_to_gitea(
    db: NocodbClient,
    gitea: GiteaClient,
    owner: str,
    repo: str,
    branch: str,
    project_id: int,
    paths: list[str],
    commit_message: str = "",
) -> list[str]:
    """Write `paths` from NocoDB virtual FS to `branch` on Gitea.

    Returns the list of paths that were successfully pushed.
    Never raises — errors are logged and skipped.
    """
    all_files = {f["path"]: f for f in db.list_project_files(project_id=project_id)}
    pushed: list[str] = []

    for path in paths:
        file_row = all_files.get(path)
        if not file_row:
            _log.warning("gitea_sync: path not in NocoDB  project=%d  path=%s", project_id, path)
            continue

        content: str = file_row.get("content") or ""
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            _log.warning("gitea_sync: skipping oversized file  path=%s  size=%d", path, len(encoded))
            continue

        try:
            _existing_content, sha = gitea.get_file_content(owner, repo, path, ref=branch)
        except Exception as exc:
            _log.debug("gitea_sync: get_file_content failed, treating as new file  path=%s  err=%s", path, exc)
            sha = ""

        msg = commit_message or f"chore: update {path}"
        try:
            gitea.put_file(owner, repo, path, content, msg, branch, sha=sha)
            pushed.append(path)
        except Exception as exc:
            _log.warning("gitea_sync: put_file failed  path=%s  err=%s", path, exc)

    return pushed
