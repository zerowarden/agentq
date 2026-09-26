# agentq

`agentq` is a local command-line tool that helps coding agents search repositories and inspect code. It returns bounded results with source locations and makes incomplete evidence explicit.

## Quick Start

Run commands from the repository you want to inspect.

`search` finds literal text and returns matching source locations.

```bash
agentq search PaymentService --path src
```

`inspect` gathers source and related evidence for a symbol, file, or source range. The `edit` intent prioritizes editable source, tests, and package ownership.

```bash
agentq inspect PaymentService --path src --intent edit
```

`continue` retrieves the next part of a truncated result. Replace `CURSOR` with the cursor returned by the previous command.

```bash
agentq continue CURSOR
```

## Requirements

- Python 3.10+
- Git
- [ripgrep](https://github.com/BurntSushi/ripgrep)
- `uv` for the installation command below

Optional: ast-grep or Universal Ctags for richer path outlines; Node.js and the target repository's TypeScript dependency for TypeScript/JavaScript inspection.

## Installation

From this repository's `agentq/` directory, install the command on your `PATH`:

```bash
uv tool install .
```
