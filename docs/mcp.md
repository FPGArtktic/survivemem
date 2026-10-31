# MCP server

`survivemem serve` runs an MCP server over stdio. The project is resolved from the
server's working directory, which Claude Code sets to the directory you started it in.

## Claude Code

Installed with `uv tool install` (the `survivemem` binary is on `PATH`):

```sh
claude mcp add --transport stdio --scope user survivemem -- survivemem serve
```

Running from a source checkout:

```sh
claude mcp add --transport stdio --scope user survivemem -- \
    uv run --project /path/to/survivemem survivemem serve
```

Options (`--transport`, `--scope`, `--env`) go before the server name; the command
goes after `--`. User scope is stored in `~/.claude.json`. Check with `claude mcp list`.

## Claude Desktop

Add the server to `claude_desktop_config.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "survivemem": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/survivemem", "survivemem", "serve"]
    }
  }
}
```

Claude Desktop for Linux is in beta (Debian/Ubuntu packages). The location of
`claude_desktop_config.json` on Linux is not documented at the time of writing; open
the file from the app's developer settings rather than guessing a path. Desktop does
not start the server in a project directory, so memories land in the global scope
unless you pass `project` to the tools or set `SURVIVEMEM_PROJECT` in the server's
`env`.

## Tools

Every description starts with `READ.` or `WRITE.` and states its preconditions.

| Tool | Access | What it does |
|---|---|---|
| `list_projects()` | read | projects, counts, identities, checkouts; marks the current ones |
| `search_memories(query, project?, limit=8)` | read | relevance-ranked hits, never a full dump |
| `store_memory(content, kind, tags?, project?, scope?, refs?, anchor?, global_memory?)` | write | only when the user asked to remember or confirmed |
| `forget_memory(id)` | write | soft delete (`survivemem restore` undoes it) |
| `list_recent(limit=20)` | read | newest first, no candidates |
| `memory_status()` | read | DB path, counts per kind, FTS health |

Errors from the store (unknown id, invalid kind, content that looks like a secret)
come back as tool errors carrying the message. The server logs to
`$XDG_STATE_HOME/survivemem/hooks.log`; stdout belongs to the protocol.
