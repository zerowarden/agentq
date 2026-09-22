import { tool } from "@opencode-ai/plugin"

const schema = tool.schema

function scriptPath(): string {
  const override = process.env.AGENTQ
  if (override) return override
  return "agentq"
}

async function invoke(argv: string[], worktree: string): Promise<string> {
  const processHandle = Bun.spawn([scriptPath(), ...argv, "--format", "text"], {
    cwd: worktree,
    stdout: "pipe",
    stderr: "pipe",
    env: { ...process.env, NO_COLOR: "1" },
  })
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
  },
  async execute(args, context) {
    const argv = ["search", args.query]
    if (args.regex) argv.push("--regex")
    for (const item of args.paths ?? []) argv.push("--path", item)
    return invoke(argv, context.worktree)
  },
})

export const inspect = tool({
  description: "Single-entry repository inspection for a symbol, literal, file, directory, or source range. Resolution ambiguities are reported as candidates rather than guessed.",
  args: {
    target: schema.string().describe("Symbol, literal, file path, or directory"),
    paths: schema.array(schema.string()).optional().describe("Repository-relative scopes"),
    intent: schema.enum(["understand", "edit"]).optional().describe("understand: declaration and references; edit: adds declaration body, tests, owning package, and verification scope"),
    lines: schema.array(schema.string()).optional().describe("START:END source ranges when target is a file"),
    candidate: schema.string().optional().describe("Opaque candidate id selected from an ambiguous edit resolution"),
  },
  async execute(args, context) {
    const argv = ["inspect", args.target]
    if (args.intent) argv.push("--intent", args.intent)
    if (args.candidate) argv.push("--candidate", args.candidate)
    for (const item of args.lines ?? []) argv.push("--lines", item)
    for (const item of args.paths ?? []) argv.push("--path", item)
    return invoke(argv, context.worktree)
  },
})
