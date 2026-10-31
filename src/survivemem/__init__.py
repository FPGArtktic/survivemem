# SPDX-License-Identifier: GPL-3.0-or-later
"""survivemem: local persistent memory for Claude Code and Claude Desktop."""


def version() -> str:
    # Looked up on demand: importing importlib.metadata costs ~20 ms, a third of a prompt hook.
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as lookup

    try:
        return lookup("survivemem")
    except PackageNotFoundError:  # running from a source tree without installation
        return "0.0.0"
