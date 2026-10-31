# SPDX-License-Identifier: GPL-3.0-or-later
"""File logging for hooks and the MCP server: stdout belongs to their protocol."""

import logging

from survivemem import paths

FORMAT = "%(asctime)s %(process)d %(name)s %(levelname)s %(message)s"


def configure(level: int = logging.INFO) -> None:
    path = paths.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
