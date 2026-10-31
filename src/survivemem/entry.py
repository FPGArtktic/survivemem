# SPDX-License-Identifier: GPL-3.0-or-later
"""Console entry point. `survivemem hook <event>` skips the CLI module and its parser: hooks
run on every prompt and tool call, and the PreToolUse budget without an investigation is 50 ms."""

import sys


def main() -> None:
    if sys.platform != "linux":
        sys.stderr.write("survivemem: Linux only\n")
        raise SystemExit(1)
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "hook":
        from survivemem import hooks

        if args[1] in hooks.HANDLERS:
            raise SystemExit(hooks.run(args[1], sys.stdin, sys.stdout))
    from survivemem import cli

    raise SystemExit(cli.run())
