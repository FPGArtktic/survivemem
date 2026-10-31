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
- on first contact with a fork whose root commit belongs to a known project, a hint
  with the `survivemem projects alias add` command that would merge them;
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

A read-only DB file still works: the project is looked up without writing, nothing
new is recorded. Without `git` on `PATH` only global memories are injected.

## `Stop` → `survivemem hook stop`

Stores the last prompt (≤ 300 chars) and the final answer (≤ 600 chars) as one
`candidate` memory. The final answer comes from `last_assistant_message`; the
prompt from the tail of the transcript, skipping tool calls, tool results, thinking
and subagent entries. Candidates are never searched or injected: promote them with
`survivemem review`. Nothing is stored when `stop_hook_active` is true or when the
text looks like it contains a secret.

## Samples

[`samples/`](samples) holds example payloads; replay one with
`survivemem hook prompt < docs/samples/user-prompt-submit.json`.
