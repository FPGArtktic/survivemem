// SPDX-License-Identifier: GPL-3.0-or-later
// Drives integrations/opencode/survivemem.js under Node: a minimal Bun.spawn shim, then the
// plugin hooks called with the payload shapes opencode 1.19 was observed to send.
import { spawn } from "node:child_process"
import { Readable } from "node:stream"

globalThis.Bun = {
  spawn(cmd, opts) {
    const child = spawn(cmd[0], cmd.slice(1), { stdio: ["pipe", "pipe", "ignore"] })
    opts.stdin.text().then((t) => child.stdin.end(t))
    return {
      stdout: Readable.toWeb(child.stdout),
      exited: new Promise((resolve) => child.on("close", resolve)),
      kill: () => child.kill(),
    }
  },
}

const [pluginPath, repo] = process.argv.slice(2)
const { SurvivememPlugin } = await import(pluginPath)
const hooks = await SurvivememPlugin({ directory: repo, worktree: repo })
const result = {}

const bash = (command) => ({ tool: "bash", sessionID: "ses_test", callID: "call_1", args: { command } })
for (let i = 0; i < 3; i++) {
  await hooks["tool.execute.after"](bash("make nonexistent-target"), {
    title: "make", output: "make: *** No rule to make target 'nonexistent-target'.  Stop.\n",
    metadata: { exit: 2 },
  })
}
try {
  await hooks["tool.execute.before"]({ tool: "bash", sessionID: "ses_test", callID: "c2" },
                                     { args: { command: "make nonexistent-target" } })
  result.blocked = null
} catch (e) {
  result.blocked = e.message
}
await hooks["tool.execute.before"]({ tool: "read", sessionID: "ses_test", callID: "c3" },
                                   { args: { filePath: "/etc/hostname" } })

const parts = [{ type: "text", id: "prt_1", text: "where does the build run?", messageID: "m", sessionID: "ses_test" }]
await hooks["chat.message"]({ sessionID: "ses_test" }, { message: {}, parts })
result.injected = parts.slice(1)

const compaction = { context: [] }
await hooks["experimental.session.compacting"]({ sessionID: "ses_test" }, compaction)
result.compaction = compaction.context
console.log(JSON.stringify(result))
