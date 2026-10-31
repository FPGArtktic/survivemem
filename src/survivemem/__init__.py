# SPDX-License-Identifier: GPL-3.0-or-later
"""survivemem: local persistent memory for Claude Code and Claude Desktop."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("survivemem")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"
