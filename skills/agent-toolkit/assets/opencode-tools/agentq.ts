import { tool } from "@opencode-ai/plugin"
import path from "node:path"

const schema = tool.schema

function scriptPath(): string {
  const override = process.env.AGENTQ
  if (override) return override
  const home = process.env.HOME
  if (!home) throw new Error("HOME is not set; set AGENTQ to the agentq executable")
  return path.join(home, ".agents", "skills", "agent-toolkit", "scripts", "agentq")
}

async function invoke(argv: string[], worktree: string): Promise<string> {
  const processHandle = Bun.spawn(
    [scriptPath(), ...argv, "--repo", worktree, "--format", "text"],
    { stdout: "pipe", stderr: "pipe", env: { ...process.env, NO_COLOR: "1" } },
  )
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(processHandle.stdout).text(),
    new Response(processHandle.stderr).text(),
    processHandle.exited,
  ])
  if (exitCode !== 0) throw new Error((stderr || stdout || `agentq exited ${exitCode}`).trim())
  return stdout.trim()
}

export const search = tool({
  description: "Bounded fixed-string or explicit-regex repository search with sensitive-path exclusion. Prefer for broad searches that might flood context.",
  args: {
    query: schema.string().describe("Exact text by default; regex only when regex=true"),
    paths: schema.array(schema.string()).optional().describe("Repository-relative scopes"),
    regex: schema.boolean().optional().describe("Enable regex semantics; default false"),
    limit: schema.number().int().min(1).max(200).optional().describe("Maximum matches; default 60"),
  },
  async execute(args, context) {
    const argv = ["search", args.query, "--limit", String(args.limit ?? 60)]
    if (args.regex) argv.push("--regex")
    for (const item of args.paths ?? []) argv.push("--path", item)
    return invoke(argv, context.worktree)
  },
})

export const inspect = tool({
  description: "Compact repository map, ranked file lookup, symbol outline, package dependencies, or lexical change-impact evidence.",
  args: {
    operation: schema.enum(["repo-map", "files", "outline", "dependencies", "impact"]),
    target: schema.string().optional().describe("Filename fragment, symbol/path, or package target as required"),
    paths: schema.array(schema.string()).optional().describe("Repository-relative scopes"),
    limit: schema.number().int().min(1).max(200).optional(),
  },
  async execute(args, context) {
    const limit = String(args.limit ?? 80)
    const argv: string[] = [args.operation]
    if (args.operation === "files") argv.push(args.target ?? "", "--limit", limit)
    else if (args.operation === "outline") {
      argv.push(...(args.paths?.length ? args.paths : ["."]), "--limit", limit)
      if (args.target) argv.push("--match", args.target)
      return invoke(argv, context.worktree)
    } else if (args.operation === "dependencies") {
      argv.push("--limit", limit)
      if (args.target) argv.push("--target", args.target)
    } else if (args.operation === "impact") {
      if (!args.target) throw new Error("impact requires target")
      argv.push(args.target, "--limit", limit)
    }
    for (const item of args.paths ?? []) argv.push("--path", item)
    return invoke(argv, context.worktree)
  },
})

export const git = tool({
  description: "Read-only compact Git status, diff, or history. Diff patch output is bounded and sensitive bodies are omitted.",
  args: {
    operation: schema.enum(["status", "diff", "history"]),
    paths: schema.array(schema.string()).optional(),
    staged: schema.boolean().optional(),
    base: schema.string().optional(),
    patch: schema.boolean().optional(),
    limit: schema.number().int().min(1).max(700).optional(),
  },
  async execute(args, context) {
    const command = args.operation === "status" ? "git-status" : args.operation === "diff" ? "git-diff" : "git-history"
    const argv = [command]
    if (args.staged && args.operation === "diff") argv.push("--staged")
    if (args.base && args.operation === "diff") argv.push("--base", args.base)
    if (args.patch && args.operation === "diff") argv.push("--patch", "--max-lines", String(args.limit ?? 500))
    if (args.operation !== "diff") argv.push("--limit", String(args.limit ?? 40))
    for (const item of args.paths ?? []) argv.push("--path", item)
    return invoke(argv, context.worktree)
  },
})
