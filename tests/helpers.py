# SPDX-License-Identifier: GPL-3.0-or-later
"""Git fixtures: throwaway repositories, clones and worktrees."""

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def make_repo(path: Path, remotes: dict[str, str] | None = None, commits: int = 1) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for i in range(commits):
        (path / "file.txt").write_text(f"{i}\n")
        git(path, "add", "file.txt")
        git(path, "commit", "-q", "-m", f"commit {i}")
    for name, url in (remotes or {}).items():
        git(path, "remote", "add", name, url)
    return path


def clone(src: Path, dst: Path, remote_url: str | None = None) -> Path:
    subprocess.run(["git", "clone", "-q", str(src), str(dst)], check=True, capture_output=True)
    if remote_url:
        git(dst, "remote", "set-url", "origin", remote_url)
    return dst
