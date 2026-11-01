// SPDX-License-Identifier: GPL-3.0-or-later
// survivemem for opencode: the Claude Code hooks, translated to opencode plugin hooks.
//
//   chat.message                     → survivemem hook prompt       (memories into the prompt)
//   tool.execute.before (bash)       → survivemem hook pretool      (loop guard: ask/deny block)
//   tool.execute.after  (bash)       → survivemem hook posttool     (capture, loop detection)
//   experimental.session.compacting  → survivemem hook session-start (state into the compaction)
//
// Install: copy to ~/.config/opencode/plugins/. The MCP server is configured separately in
// opencode.json. Every call is bounded and failures are ignored: memory never blocks opencode.

const EXE = process.env.SURVIVEMEM_BIN || "survivemem"
const TIMEOUT_MS = 5000

async function hook(event, payload) {
  try {
    const proc = Bun.spawn([EXE, "hook", event], {
      stdin: new Blob([JSON.stringify(payload)]),
      stdout: "pipe",
      stderr: "ignore",
    })
    const timer = setTimeout(() => proc.kill(), TIMEOUT_MS)
    const out = await new Response(proc.stdout).text()
    clearTimeout(timer)
    await proc.exited
    return out.trim()
  } catch {
    return "" // survivemem missing or crashed: behave as if there was nothing to say
  }
}

function promptText(parts) {
  return parts
    .filter((p) => p.type === "text" && !p.synthetic)
    .map((p) => p.text)
    .join("\n")
}

export const SurvivememPlugin = async ({ directory, worktree }) => {
  const cwd = worktree || directory
  return {
    "chat.message": async (input, output) => {
      const out = await hook("prompt", {
        cwd,
        session_id: input.sessionID,
        prompt: promptText(output.parts),
      })
      const first = output.parts.find((p) => p.type === "text")
      if (!out || !first) return
      output.parts.push({ ...first, id: `${first.id}sm`, text: out, synthetic: true })
    },

    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash" || !output.args?.command) return
      const out = await hook("pretool", {
        cwd,
        session_id: input.sessionID,
        tool_name: "Bash",
        tool_input: { command: output.args.command },
      })
      if (!out) return
      const decision = JSON.parse(out).hookSpecificOutput ?? {}
      // opencode cannot ask from a plugin: both "ask" and "deny" block, with the reason
      if (decision.permissionDecision === "ask" || decision.permissionDecision === "deny") {
        throw new Error(decision.permissionDecisionReason)
      }
    },

    "tool.execute.after": async (input, output) => {
      if (input.tool !== "bash" || !input.args?.command) return
      const exit = output.metadata?.exit ?? 0
      const text = output.output ?? ""
      const base = {
        cwd,
        session_id: input.sessionID,
        tool_name: "Bash",
        tool_input: { command: input.args.command },
        tool_use_id: input.callID,
      }
      await hook(
        "posttool",
        exit === 0
          ? { ...base, hook_event_name: "PostToolUse", tool_response: { stdout: text, stderr: "" } }
          : { ...base, hook_event_name: "PostToolUseFailure", error: `Exit code ${exit}\n${text}` },
      )
    },

    "experimental.session.compacting": async (input, output) => {
      const out = await hook("session-start", { cwd, session_id: input.sessionID, source: "compact" })
      if (out) output.context.push(out)
    },
  }
}
