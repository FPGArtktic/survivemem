# Claude Code hooks

Every hook is the same binary: `survivemem hook <event>` reads the hook's JSON payload
on stdin. A hook never fails: errors are logged with a traceback to
`$XDG_STATE_HOME/survivemem/hooks.log` and the process exits 0. Hooks never import
the MCP SDK and never touch the network.

Install by merging [`settings.json`](settings.json) into `~/.claude/settings.json`
(all projects) or `.claude/settings.json` (one project).

## `UserPromptSubmit` → `survivemem hook prompt`

Resolves the project from `cwd`, records the current clone, and prints on stdout
(Claude Code adds it to the context):

- one line naming this clone and the other clones of the project, when more than one
  exists on disk:
  `linux · this clone ~/w/linux-de10 @socfpga-6.6 (abc1234, .config 9f3e…) · others: ~/w/linux-up @master`
- on first contact with a fork that was not merged automatically (a fork of a
  `families.toml` upstream such as the kernel or U-Boot, or any fork with
  `root_match = "propose"`), a hint with the `survivemem projects alias add` command that
  would merge them;
- up to 8 relevant memories, ≤ 400 characters each, ≤ 6 000 characters in total:

```
<survivemem>
- [decision] Kernel pinned to 6.6 LTS; vendor BSP breaks on 6.12. (01JB4K2Q)
- [environment] /dev/ttyUSB1=de10-nano-uart [clone linux-de10] (01JB4M7A)
</survivemem>
```

Nothing is printed when nothing is relevant. When the prompt looks like an error
(`ERROR`, `failed`, `Oops`, `panic`, `timing`, `slack`, `Critical Warning`,
`Error (`, `Fitter`), `pitfall` memories are ranked first. FTS5 only, no embedding
call; on a DB with 10 000 memories the whole process takes about 60 ms.

The prompt hook also records the session id as the most recently active session of the
project; the MCP `checkpoint` tool writes to that session.

A read-only DB file still works: the project is looked up without writing, nothing
new is recorded. Without `git` on `PATH` only global memories are injected.

## `Stop` → `survivemem hook stop`

Stores the last prompt (≤ 300 chars) and the final answer (≤ 600 chars) as one
`candidate` memory. The final answer comes from `last_assistant_message`; the
prompt from the tail of the transcript, skipping tool calls, tool results, thinking
and subagent entries. Candidates are never searched or injected: promote them with
`survivemem review`. Nothing is stored when `stop_hook_active` is true or when the
text looks like it contains a secret.

## `PreCompact` → `survivemem hook precompact`

The deterministic fallback for a model that forgot to call `checkpoint`. From the tail
of the transcript it extracts the last 3 prompts (≤ 300 chars), the last answer
(≤ 600 chars), the files touched by `Edit`/`Write`/`Read` (newest first, ≤ 30), the last
10 `Bash` commands and the last `TodoWrite` list, and merges them into the session's
state: the model's goal and decisions always win, files are merged, todos refresh
done / in progress / next. It also stores a candidate like the Stop hook, and purges
session states older than 14 days. It never blocks compaction.

## `SessionStart` (matcher `compact|resume`) → `survivemem hook session-start`

Right after a compaction or a resume, prints:

1. the clone line, even for a single clone (compaction loses the `cwd` context);
2. `<survivemem-state>` — the session's checkpoint, terse, ≤ 3 000 characters:

```
<survivemem-state>
GOAL: UART console bring-up on de10-nano
IN PROGRESS: probe failure of altera_uart
NEXT:
- check the clock in socfpga.dtsi
DECISIONS:
- stay on 6.6 LTS: vendor BSP breaks on 6.12
FILES:
- arch/arm/boot/dts/socfpga_cyclone5_de10_nano.dts
</survivemem-state>
```

3. `<survivemem>` — up to 8 long-term memories for the goal and next steps, topped up
   with the newest ones, ≤ 3 000 characters.

Nothing is printed when nothing is stored and the directory is not a project.

## Samples

[`samples/`](samples) holds example payloads; replay one with
`survivemem hook prompt < docs/samples/user-prompt-submit.json`.
