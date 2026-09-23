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
  description: "Single-entry repository inspection for a symbol, existing path, directory, or source range; literal content belongs to search. Ambiguous symbols return candidate ids instead of a guessed declaration.",
  args: {
    target: schema.string().describe("Symbol name, existing repository-relative path, or symbol:/path: prefixed selector"),
    paths: schema.array(schema.string()).optional().describe("Repository-relative evidence scopes"),
    intent: schema.enum(["understand", "edit", "rename", "refactor", "impact"]).optional().describe("Evidence emphasis: understand context, edit source and tests, rename references and mentions, refactor implementations, impact dependents and ownership"),
    lines: schema.array(schema.string()).optional().describe("START:END source ranges when target is a file"),
    line: schema.number().optional().describe("Single source anchor when target is a file"),
    column: schema.number().optional().describe("One-based column with a single line; expresses an exact location"),
    candidate: schema.string().optional().describe("Opaque candidate id selected from an ambiguous resolution"),
  },
  async execute(args, context) {
    const argv = ["inspect", args.target]
    if (args.intent) argv.push("--intent", args.intent)
    if (args.candidate) argv.push("--candidate", args.candidate)
    if (args.line !== undefined) argv.push("--line", String(args.line))
    if (args.column !== undefined) argv.push("--column", String(args.column))
    for (const item of args.lines ?? []) argv.push("--lines", item)
    for (const item of args.paths ?? []) argv.push("--path", item)
    return invoke(argv, context.worktree)
  },
})
