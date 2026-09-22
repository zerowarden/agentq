# Optional OpenCode Custom Tools

The adapter exposes two read-only tools: `agentq_search` and `agentq_inspect`.

Do **not** install it by default. OpenCode already has built-in search, read, Bash, and LSP tools; custom tool schemas consume model context. Install this adapter only when empirical testing shows that a local model ignores skill instructions or repeatedly produces unbounded shell output.

Install with:

```bash
~/.agents/agentq/integrations/opencode/install-opencode-tools.sh
~/.agents/agentq/integrations/opencode/install-opencode-tools.sh --apply
```

The adapter executes the local `agentq` binary using argv arrays, not a shell. It does not expose mutation, arbitrary command execution, or network access.
