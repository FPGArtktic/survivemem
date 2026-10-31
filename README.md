# survivemem

Local, single-user persistent memory for Claude Code and Claude Desktop.
One SQLite file, no server, no API keys, no telemetry.

Linux only. Licensed under GPL-3.0-or-later.

## What it is and why

survivemem keeps the useful part of hosted "AI memory" on your own machine: durable
facts, preferences and project decisions, injected into Claude's context when they are
relevant, and the working state of a session, put back after the context is compacted.

It is built around three problems that show up in long engineering sessions:

- **Compaction drops the plot.** `/compact` and auto-compaction replace the
  conversation with a summary that regularly loses the goal, the decisions already
  taken and the open items. survivemem stores a checkpoint of that state in SQLite
  (written by the model through MCP, with a deterministic fallback extracted from the
  transcript) and re-injects it right after every compaction.
  See [docs/compaction.md](docs/compaction.md).
- **One repository, many clones.** Kernels, U-Boot, Yocto layers and BSPs get cloned
  several times: one per board, one per branch under review, a bisect scratch copy,
  your own fork. survivemem recognises a project by its remotes, its `.survivemem.toml`
  name and its root commit, not by its directory, so memories are shared across clones
  and forks, while each clone's branch and `.config` stay visible.
  See [docs/identity.md](docs/identity.md).
- **Relevant, bounded injection.** Every prompt gets the few memories that match it
  (FTS5 with an identifier-friendly tokenizer, at most 8 hits and 6 000 characters),
  not a dump of everything stored.

## Features

- Memories with a kind (`fact`, `preference`, `project`, `decision`, `environment`,
  `pitfall`), tags, an optional source pointer (`refs`) and anchor, stored per project
  or globally.
- Scopes: a memory can hold for the whole project, one branch (`branch:<name>`) or one
  clone (`checkout:<id>`).
- Search: FTS5 `bm25` with a tokenizer that keeps identifiers such as
  `CONFIG_ARM64_PTR_AUTH`, `linux-6.6.y` or `0xFF200000` whole, trigram fallback for
  substrings, Reciprocal Rank Fusion, optional vector rerank (`sqlite-vec` and a local
  Ollama). In the prompt hook, pitfalls rank first when the prompt looks like an error.
- Claude Code hooks: inject memories on every prompt, capture candidates when a turn
  ends, save the session before compaction, restore it after, stop repeated failed
  experiments before they run, capture failing debug commands.
- Investigations: debug sessions that span days and compactions, with hypotheses,
  experiments, ruled-out causes and evidence, re-injected after every compaction and
  guarded against loops.
- Candidates are captured automatically but never searched or injected until you
  promote them with `survivemem review`.
- MCP server (stdio) with twelve tools to search, store, forget, list, checkpoint and
  run investigations.
- Workspaces: link the repositories of a Yocto build, a kas file or a repo manifest so
  their memories are visible from each other (`projects autodetect`).
- A scriptable CLI: human tables on a terminal, JSON everywhere else, stable exit
  codes, a JSON Schema of every object (`survivemem schema`).
- Export to JSONL (with project identities, so a mirror on another machine is
  recognised) or Markdown; idempotent import.
- `survivemem doctor`: integrity, FTS, filesystem, configuration and integration
  checks; `--stale` lists memories whose anchor no longer matches the clone.
- Deterministic log extraction for BitBake, kernel `make`, `dmesg`, Quartus and
  OpenOCD (`survivemem ingest-log`).

### Not available yet

- Shared memories committed in a repository (`[share]` in the configuration is parsed
  but not used).

## Requirements

- Linux (glibc, x86_64 or aarch64). Other platforms exit with `survivemem: Linux only`.
- Python 3.12 or newer, with an SQLite that has FTS5 (the usual case; without FTS5
  the database cannot be created).
- [uv](https://docs.astral.sh/uv/) to install and run.
- `git` on `PATH` for project identity (without it, only a `.survivemem.toml` names a
  project and everything else falls back to global memories); the OpenSSH client is
  used, if present, to resolve SSH host aliases.
- Claude Code and/or Claude Desktop.
- Optional, for vector search: the `vec` extra (`sqlite-vec`) and
  [Ollama](https://ollama.com) on `127.0.0.1` with the `bge-m3` model.

## Install

Straight from GitHub:

```sh
uv tool install git+https://github.com/FPGArtktic/survivemem
```

From a checkout:

```sh
git clone https://github.com/FPGArtktic/survivemem
cd survivemem
uv tool install .
```

With the optional vector search extra:

```sh
uv tool install 'survivemem[vec] @ git+https://github.com/FPGArtktic/survivemem'
# or, from a checkout
uv tool install '.[vec]'
```

`uv tool install` puts `survivemem` in uv's tool bin directory (usually
`~/.local/bin`); run `uv tool update-shell` if that is not on your `PATH`. The hooks
call `survivemem` by name, so it must be on the `PATH` Claude Code sees. To update,
re-run the install command with `--reinstall`.

Check the installation:

```sh
survivemem --version
survivemem doctor
```

## Quick start

```sh
cd ~/w/linux-de10
survivemem add "Kernel pinned to 6.6 LTS; vendor BSP breaks on 6.12." --kind decision --tag kernel
survivemem add "Prefer ruff over black" --kind preference --global
survivemem search "kernel 6.6"
survivemem list --since 7d
survivemem projects here        # which project this directory is, and why
```

```
id        kind        project     updated  tags    content
01M68KH5  preference  global      now              Prefer ruff over black
01M68KH5  decision    linux-de10  now      kernel  Kernel pinned to 6.6 LTS; vendor BSP breaks on 6.12.
```

The project is derived from the current directory; nothing needs to be registered.
`--global` stores a memory that is visible from everywhere. IDs are ULIDs and every
command accepts any unique prefix (`survivemem show 01M68KH5J1`).

## Claude Code setup

### MCP server

```sh
claude mcp add --transport stdio --scope user survivemem -- survivemem serve
```

Check with `claude mcp list`. Claude Code starts the server in the directory you
launched it from, which is how the server knows the project. See
[docs/mcp.md](docs/mcp.md) for the tool list and for running from a source checkout.

### Hooks

Merge [`docs/settings.json`](docs/settings.json) into `~/.claude/settings.json` (all
projects) or `.claude/settings.json` (one project). If the file already has a `hooks`
object, add these entries to it rather than replacing it:

| Event | Matcher | Command | Timeout |
|---|---|---|---|
| `UserPromptSubmit` | | `survivemem hook prompt` | 5 s |
| `PreToolUse` | `Bash` | `survivemem hook pretool` | 5 s |
| `PostToolUse` | `Bash\|mcp__.*` | `survivemem hook posttool` | 5 s |
| `PostToolUseFailure` | `Bash` | `survivemem hook posttool` | 5 s |
| `Stop` | | `survivemem hook stop` | 5 s |
| `PreCompact` | | `survivemem hook precompact` | **30 s** (room for the optional local summary) |
| `SessionStart` | `compact\|resume` | `survivemem hook session-start` | 5 s |

`PreToolUse` costs one SQLite query per Bash call when no investigation is open
(about 45 ms for the whole process). `PostToolUse` opens the database only for Bash
commands matching `[capture] debug_commands` and for the configured MCP tools.
`PostToolUseFailure` is needed because a failed Bash call does not fire `PostToolUse`.

Hooks never fail: errors go to `~/.local/state/survivemem/hooks.log` and the exit
code is always 0. `survivemem status` and `survivemem doctor` show which hook events
are registered. Details and an activity diagram per hook: [docs/hooks.md](docs/hooks.md).

The model is told through the MCP server's instructions and tool descriptions when to
search, when it may store (only when you ask or confirm), and to call `checkpoint` after
each completed subtask. A line in your `CLAUDE.md` such as "call the survivemem
`checkpoint` tool after every completed subtask" makes the habit more reliable.

## Claude Desktop

Claude Desktop for Linux is in beta. Add the server to `claude_desktop_config.json`
under `mcpServers` (open the file from the app's developer settings; its location on
Linux is not documented at the time of writing):

```json
{
  "mcpServers": {
    "survivemem": {
      "command": "/home/you/.local/bin/survivemem",
      "args": ["serve"],
      "env": { "SURVIVEMEM_PROJECT": "linux-de10" }
    }
  }
}
```

Use the absolute path printed by `command -v survivemem`: a desktop application does
not necessarily see the `PATH` of your shell. Desktop runs no hooks, so nothing is
injected automatically and there is no loop guard: the model searches and reads
investigations through the MCP tools. It also does not start the server in a project directory;
without `SURVIVEMEM_PROJECT` (a project label or id) or a `project` argument to the
tools, memories land in the global scope. `checkpoint` and `session_state` work, using
a stable per-project session id.

## How it works

```
prompt ──► hook prompt ──► resolve project ──► FTS5 search ──► <survivemem> block in context
model  ──► MCP checkpoint ──► session_state
/compact ──► hook precompact (merge transcript heuristic) ──► compaction ──► hook session-start
          ──► clone line + <survivemem-state> + <survivemem> back in context
turn ends ──► hook stop ──► candidate ──► survivemem review ──► memory
Bash call ──► hook pretool ──► loop guard (only with an open investigation)
          ──► hook posttool ──► evidence + experiment entry, or candidate
```

- [docs/architecture.md](docs/architecture.md): components and deployment
- [docs/identity.md](docs/identity.md): project resolution, URL normalisation, clones, scopes
- [docs/hooks.md](docs/hooks.md): each hook, its output and failure behaviour
- [docs/compaction.md](docs/compaction.md): the checkpoint round trip and merge rules
- [docs/investigations.md](docs/investigations.md): debug sessions, the loop guard, log extraction
- [docs/mcp.md](docs/mcp.md): MCP registration and tools
- [docs/data-model.md](docs/data-model.md): the SQLite schema

## Daemon in a container (optional)

By default hooks, CLI and MCP server open the database directly. Profile B runs one
long-lived daemon instead (MCP over Streamable HTTP on `127.0.0.1:8765`, hooks as thin
clients with `--remote`, a bearer token next to the DB), packaged as a rootless Podman
Quadlet unit:

```sh
podman build -t localhost/survivemem:latest .
cp deploy/survivemem.container ~/.config/containers/systemd/
systemctl --user daemon-reload && systemctl --user start survivemem
```

When the daemon is down, `--remote` hooks fall back to the database. Setup, diagrams,
security model and limitations: [docs/deployment.md](docs/deployment.md).

## Investigations

For debugging that spans days and compactions (board bring-up, kernel regressions,
timing failures), open an investigation. It records the symptom, the setup under test,
hypotheses, experiments with their command, result and anchor, what was ruled out, and
evidence excerpts:

```sh
survivemem inv open "altera_uart probe fails" --symptom "no console on ttyS1 after boot"
survivemem inv log hypothesis "uart clock not enabled before probe"
survivemem inv log experiment "rebuild the dtb with a clocks property" --command "make -j8 dtbs"
survivemem inv log ruled_out "pinmux is fine: scope shows TX toggling"
survivemem inv show
survivemem inv close --root-cause "…" --fix "…"     # solved: writes a pitfall memory
```

The model does the same through the four `investigation_*` MCP tools. The hooks then:

- re-inject the investigation block after every compaction, and on the first and
  every 20th prompt of a session;
- record each failing debug command (`make`, `bitbake`, `dmesg`, Quartus, OpenOCD, …)
  as an experiment with an output excerpt;
- ask you before Claude re-runs a command that already failed at the same commit and
  `.config`, unless a new hypothesis was logged;
- flag the third identical failure as a loop, shown on every prompt until a new
  hypothesis answers it.

Details, diagrams and the log extraction rules: [docs/investigations.md](docs/investigations.md).

## CLI overview

`survivemem --help` prints a summary; `survivemem help <command>` the options of one
command.

| Command | Purpose |
|---|---|
| `add "<text>" [--kind K] [--tag T]… [--scope project\|branch\|checkout] [--ref JSON]… [--anchor REV]` | store a memory (default kind `fact`) in the current project, `--project X` or `--global` |
| `search "<query>" [--limit 8] [--explain]` | relevance-ranked search; `--explain` adds bm25, vector distance and match source |
| `list [--kind K] [--tag T] [--since 30m\|12h\|7d\|2w] [--limit N]` | newest first; candidates only with `--kind candidate` |
| `show <id>` | full record, including deleted ones, refs as commands |
| `edit <id> [--content …] [--kind …] [--add-tag …] [--rm-tag …] [--project X\|--global] [--scope …] [--ref JSON]… [--anchor …]` | change a memory |
| `forget <id>… [--yes]`, `restore <id>` | soft delete and undo |
| `review` | walk candidates interactively and promote, edit or discard them |
| `projects` | list projects with their primary identity and counts |
| `projects here` | resolution chain for `$PWD`: identities, matched step, remotes, clone |
| `projects show\|rename\|merge\|identities …` | inspect and fix projects |
| `projects alias add <project> <identity> [--yes]`, `projects alias rm <identity>` | attach or detach `remote:`, `root:`, `path:`, `name:` identities |
| `projects link\|unlink <parent> <child>`, `projects autodetect [--project X] [--yes]` | workspace links, by hand or from `bblayers.conf`, `kas*.yml`, `.repo/manifest.xml` |
| `clones [--all]`, `clones label <id> "<text>"`, `clones prune [--purge] [--yes]`, `clones diff <a> <b>` | the clones (checkouts) of a project |
| `status` | DB path and size, counts, FTS/vector/Ollama state, hook and MCP registration |
| `brief` | one JSON document for an AI client to orient itself |
| `schema` | JSON Schema of memory, project, checkout, checkpoint and ref objects |
| `env` | `environment` memories as a KEY / value table |
| `inv open "<title>" --symptom "<text>" [--env TEXT]` | open an investigation (one open per project) |
| `inv log <kind> "<text>" [--command C] [--result good\|bad\|inconclusive] [--anchor A] [--ref JSON]… [--evidence FILE]` | append a hypothesis, experiment, observation, ruled_out or note |
| `inv show [<id>] [--full]`, `inv list [--status S] [--grep TEXT] [--all]` | the investigation block or timeline; list investigations |
| `inv attach <file> [--tool T] [--no-copy]`, `inv bisect`, `inv loops` | evidence; good/bad boundary; repeated identical failures |
| `inv pause`, `inv resume [<id>]`, `inv close --root-cause … --fix … [--status solved\|abandoned]`, `inv export [<id>]` | lifecycle and Markdown postmortem |
| `ingest-log <file> --tool bitbake\|make\|dmesg\|quartus\|openocd\|gdb\|system-console\|other` | extract errors from a log into evidence or a candidate |
| `state [show] [--session ID]`, `state set …`, `state purge [--all] --yes` | the compaction-proof session checkpoint |
| `export [--format jsonl\|md] [--kind K]`, `import < file.jsonl` | backup, migration, Markdown notes |
| `reindex` | rebuild the FTS indexes; (re)build vectors when available |
| `doctor [--stale]` | health checks, exit code 3 when one fails; `--stale` lists memories whose anchor no longer matches the current clone |
| `serve` | MCP server over stdio |
| `hook prompt\|pretool\|posttool\|stop\|precompact\|session-start` | Claude Code hook handlers (JSON on stdin) |

Filters on `add`, `list`, `search`, `review`, `brief`, `env`, `export`, `clones` and
`inv`:
`--project <label|id>`, `--global` (only project-less memories, or store one),
`--all` (no project filter). `export` covers all projects unless filtered.

`review` keys: `f` fact, `p` preference, `d` decision, `t` pitfall, `v` environment,
`j` project, `e` edit the text first, `x` discard, `s` or Enter skip, `q` quit.

## Output modes and exit codes

| Option | Effect |
|---|---|
| (default) | a table or plain text on a terminal; JSON when stdout is not a terminal |
| `--json` / `--jsonl` | one JSON document / one object per line |
| `--format table\|md\|json\|jsonl` | explicit format; `md` gives Markdown for `list`, `search` and `projects` |
| `--quiet`, `-q` | IDs only, one per line, for `xargs` |
| `--wide` | do not truncate the content column |
| `--no-color`, `NO_COLOR` | plain ASCII (`...` instead of `…`); no ANSI colour is ever emitted |

Long output on a terminal goes through `$PAGER` (default `less -R`). JSON objects use
the database column names plus an `*_iso` string for every timestamp
([docs/data-model.md](docs/data-model.md#json-representation)). Commands never prompt
when stdin is not a terminal: destructive ones (`forget`, `projects merge`,
`clones prune --purge`, `state purge`) refuse without `--yes`, and `review` refuses to
run.

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | usage error, invalid input or configuration, refused without `--yes` |
| 2 | not found |
| 3 | database error (including a failed `doctor` check) |
| 4 | ambiguous ID prefix |

Errors go to stderr; in machine mode as `{"error": {"code": …, "message": …}}`.

## Configuration

`$XDG_CONFIG_HOME/survivemem/config.toml` (default `~/.config/survivemem/config.toml`,
override with `SURVIVEMEM_CONFIG`). Every key is optional; a missing file means the
defaults below. An invalid value makes the CLI exit with code 1; hooks log the error
and use the defaults.

```toml
[identity]
# Remote whose identity is checked first and named in hints; then "upstream", then the
# rest alphabetically.
remote = "origin"
# Forks whose root commit is NOT in families.toml: "adopt" (same project) or "propose"
# (new project plus a one-line hint with the merge command).
root_match = "adopt"

[identity.hosts]
# Rewrite "host:port" or "host" before remote URLs are compared. Empty by default.
# "gitlab.lan:12345" = "gitlab.lan"
# "192.168.1.10" = "gitlab.lan"

# Forks of a families.toml upstream (linux, u-boot, buildroot, openembedded-core,
# trusted-firmware-a, busybox) default to "propose"; override per family label.
# [identity.family.u-boot]
# root_match = "adopt"

[compaction]
# Ask a local Ollama model for a 10-line session summary in the PreCompact hook.
summarize = false
model = "llama3.2"

[embed]
# Ollama embedding model for vector search; must return 1024-dimensional vectors.
model = "bge-m3"
# How long Ollama keeps the model loaded after each request. Loading it cold takes longer
# than the 500 ms search budget, and search then quietly falls back to FTS5 only.
keep_alive = "30m"

[loop_guard]
# What the PreToolUse hook does with a command identical to a failed experiment at the
# same anchor: "ask" (Claude Code asks you), "deny" (refused), "warn" (context only).
mode = "ask"

[capture]
# Regular expression matched in FULL against the program name of each segment of a Bash
# command line (after sudo/env/VAR=…): "make" matches make and /usr/bin/make, not cmake,
# makepkg or `grep make`.
debug_commands = "bitbake|devtool|make|dmesg|journalctl|openocd|gdb|gdb-multiarch|[\\w.-]+-gdb|quartus_\\w+|jtagconfig|system-console|nios2-[\\w-]+|niosv[\\w-]*|bmaptool|dd"
# Shell-style patterns for MCP tools whose results are captured as candidates.
tools = ["mcp__jcodemunch__*", "mcp__rag__*", "mcp__jdocmunch__*"]

[share]
# Parsed and validated, but not used: shared repository memories are not implemented.
enabled = false
```

A repository can pin its project name with a `.survivemem.toml` at its root:

```toml
project = "de10-nano-bsp"
```

### Environment variables

| Variable | Effect |
|---|---|
| `SURVIVEMEM_DB` | database path (default `$XDG_DATA_HOME/survivemem/memory.db`) |
| `SURVIVEMEM_CONFIG` | configuration file path |
| `SURVIVEMEM_PROJECT` | force the project (label or id) for hooks, CLI and MCP server; created if unknown |
| `SURVIVEMEM_OLLAMA_URL` | Ollama base URL, default `http://127.0.0.1:11434`; only `http` on `127.0.0.1`, `localhost` or `::1` is accepted |
| `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` | base directories; relative values are ignored |
| `NO_COLOR`, `PAGER` | output, see above |

## Vector search (optional)

FTS5 alone works well for technical text. Vector search adds semantic matches through
a local embedding model:

```sh
uv tool install --reinstall 'survivemem[vec] @ git+https://github.com/FPGArtktic/survivemem'
ollama pull bge-m3
survivemem reindex       # creates memory_vec and embeds every live memory
survivemem status        # vec: enabled (sqlite-vec loaded, N embedded)
```

- `survivemem search` and the MCP `search_memories` embed the query (500 ms timeout)
  and fuse the cosine top 50 with the lexical ranking by Reciprocal Rank Fusion.
- The prompt hook never embeds; it stays FTS5 only to keep each prompt fast.
- The Stop hook embeds up to 16 new or changed memories per turn (2 s timeout).
- If `sqlite-vec` is missing, Ollama is down or slow, or the model is not pulled,
  everything silently falls back to FTS5. `reindex` says why vectors stay disabled.
- Run `survivemem reindex` again after changing `[embed] model`.

## Privacy

- **No network**, except an optional Ollama on loopback. Any other Ollama URL is
  refused and logged, and `HTTP_PROXY`/`ALL_PROXY` are ignored for it, so memory text
  never goes through a proxy. `ssh -G` reads your SSH configuration without connecting. The
  `$schema` URI in `survivemem schema` is an identifier and is never fetched. No
  telemetry.
- **Secrets are rejected.** Memories and captured candidates that match obvious
  token or key patterns (GitHub, GitLab, OpenAI/Anthropic, AWS, Google and Slack keys,
  private-key headers, `password=` / `api_key=`-style assignments) are refused by
  every write path (CLI, MCP, hooks, import).
- **What hooks keep.** The Stop and PreCompact hooks store the last prompt (300
  characters) and the final answer (600 characters) of a turn as a candidate. The
  session state additionally holds the last 3 prompts, the last answer, the last 10
  Bash command lines and the paths of files Claude read or edited, with secrets
  redacted; it is never exported or searched and is purged after 14 days without
  activity. The PostToolUse hook keeps, for matching debug commands, an excerpt of at
  most 1 000 characters of the output (redacted), and for matching MCP tools a digest
  of at most 300 characters plus pointers; never the full output, never file contents.
- Everything is in one file you own: copy it, back it up, or delete it.

## Files and paths

| Path | Content |
|---|---|
| `~/.local/share/survivemem/memory.db` (`$XDG_DATA_HOME`, `SURVIVEMEM_DB`) | the database, plus its `-wal` and `-shm` files while in use |
| `memory.db.bak-<version>` next to it | copy taken before a schema migration |
| `~/.config/survivemem/config.toml` (`$XDG_CONFIG_HOME`, `SURVIVEMEM_CONFIG`) | configuration |
| `~/.local/state/survivemem/hooks.log` (`$XDG_STATE_HOME`) | log of the hooks and the MCP server; appended, never rotated |
| `<repo>/.survivemem.toml` | optional project name for a repository |

Keep the database on a local filesystem: WAL locking is unreliable on NFS, CIFS,
sshfs and 9p, and `survivemem doctor` fails there.

## Troubleshooting

Start with:

```sh
survivemem doctor
```

```
OK   integrity   ok
OK   schema      v3, code expects v3
OK   fts         memories_fts and memories_tri consistent
OK   filesystem  ext4
OK   config      defaults (no config.toml)
OK   vec         disabled (no memory_vec (run reindex)); search uses FTS5 only
OK   ollama      http://127.0.0.1:11434 unreachable; vectors and summaries off
OK   hooks       all hook events registered
OK   mcp         survivemem MCP server registered
OK   names       no .survivemem.toml name collisions
OK   clones      3 clones recorded
OK   profile     A (native): hooks, CLI and MCP server open the DB directly
```

| Symptom | What to check |
|---|---|
| Nothing is injected | Run `survivemem search "<words from the prompt>"` in the same directory. Candidates are never injected; promote them with `survivemem review`. A prompt made only of common words matches nothing. Branch- and clone-scoped memories are only visible from that branch or clone. |
| Wrong or new project | `survivemem projects here` shows the identities, the matched step and any root-commit hint; fix with `projects merge` or `projects alias add` ([docs/identity.md](docs/identity.md#fixing-identification)). |
| A hook seems to do nothing | Look at `~/.local/state/survivemem/hooks.log`; every failure is logged there with a traceback. Replay a payload: edit `cwd` in `docs/samples/user-prompt-submit.json`, then `survivemem hook prompt < docs/samples/user-prompt-submit.json`. |
| `fts ... out of sync` | `survivemem reindex` |
| `database schema vN is newer than this survivemem` | install a newer survivemem, or restore `memory.db.bak-<version>` |
| Read-only database | Hooks, CLI and MCP server fall back to a lookup that writes nothing: memories are still injected and read commands work, writes fail with `attempt to write a readonly database` (exit code 3). Fix the file permissions. |
| The loop guard asks about a command you meant to repeat | Log why the outcome should differ (`survivemem inv log hypothesis "…"`), or check what it matches with `survivemem inv loops` ([docs/investigations.md](docs/investigations.md)). |
| `survivemem: Linux only` | survivemem does not run on other platforms. |

## Development

```sh
git clone https://github.com/FPGArtktic/survivemem
cd survivemem
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

- Every test gets its own `HOME`, XDG directories and database, and points
  `SURVIVEMEM_OLLAMA_URL` at a closed port, so a running Ollama or your real memory
  database is never touched.
- The development group installs `sqlite-vec` so the vector path is tested; Ollama is
  replaced by a fake embedder.
- Table and Markdown renderings are pinned by golden files; regenerate them with
  `UPDATE_GOLDEN=1 uv run pytest` and review the diff.
- Run the CLI from the checkout with `uv run survivemem …`. Use `SURVIVEMEM_DB` and
  `XDG_STATE_HOME` to point experiments at a scratch database.

## Author

Mateusz Okulanis, GitHub [FPGArtktic](https://github.com/FPGArtktic),
e-mail FPGArtktic@outlook.com.

Bug reports, questions and pull requests:
<https://github.com/FPGArtktic/survivemem/issues>.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
