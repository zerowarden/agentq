# Local CLI Tooling Policy

The bundle is intentionally useful with only **Git**, **ripgrep**, and **Python 3.10+**. Optional tools are selected for local execution, mature FOSS licensing, bounded or machine-readable output, and a narrow purpose that improves reliability rather than merely adding novelty.

## Recommended tiers

| Tier | Tool | Purpose in this bundle | Installation preference |
|---|---|---|---|
| Required | Git | authoritative change/status/history data | Kubuntu package |
| Required | ripgrep (`rg`) | fast ignored-file-aware search with JSON events | Kubuntu package |
| Required | Python 3.10+ | dependency-free `agentq` runtime | Kubuntu package |
| Recommended | ast-grep | syntax-aware search, outlines, and guarded codemods | `cargo install --locked ast-grep` |
| Recommended | Universal Ctags | broad cross-language symbol inventory fallback with JSON Lines | Kubuntu `universal-ctags` package |
| Recommended | Difftastic (`difft`) | syntax-aware single-file diffs | `cargo install --locked difftastic` |
| Recommended | Hyperfine | warmups, repeated timing, statistics, JSON export | Kubuntu package if available, otherwise Cargo |
| Useful | ShellCheck | static analysis for shell scripts | Kubuntu package |
| Useful | shfmt | deterministic shell formatting | Kubuntu package |
| Useful | jq | manual inspection of `--format json` output | Kubuntu package |
| Useful | Tokei | fast local code statistics | Kubuntu package if available, otherwise Cargo |
| Optional | fd (`fdfind` on Debian/Ubuntu) | ergonomic human filename discovery | Kubuntu `fd-find` package |
| Optional | Gitleaks | local repository or directory secret scanning | official binary, Go install, or pinned pre-commit hook |

## Why these tools

- **ripgrep** respects ignore files by default and provides structured JSON events, which makes hard output caps possible without parsing display-oriented text.
- **ast-grep** matches syntax trees rather than raw strings and supports structural rewrite. On Linux, use the full `ast-grep` binary name because `/usr/bin/sg` is commonly the unrelated `setgroups` command.
- **Universal Ctags** can emit one JSON object per tag and accepts a file list, avoiding shell argument-length failures.
- **Difftastic** makes selected dense code diffs easier to understand, but it is deliberately restricted to one file at a time in this bundle.
- **Hyperfine** performs warmups and repeated measurements. Its output is reduced to the statistics required for comparison.
- **Gitleaks** is local and redaction-aware, but it is not invoked automatically: repositories need their own allowlists/baselines, and a secret scan is materially different from a generic patch heuristic.

## Tools deliberately not required

- `tree`, `find`, raw `grep`, and unbounded `git diff` remain available, but are not default agent interfaces because their display output can expand without a useful semantic stopping rule.
- `bat`, `delta`, `fzf`, and other terminal presentation tools are useful for humans but usually add ANSI/layout tokens without improving model evidence.
- Runtime `npx`/`pnpx` downloads are avoided. Install a project-specific TypeScript analyzer such as Knip as a pinned dev dependency in the repository when the project adopts it; do not fetch it ad hoc during an agent task.
- A generic static analyzer is not silently substituted for project-native ESLint, TypeScript, Ruff, Clippy, database, or security checks.

## Network and privacy boundary

`agentq` does not initiate network access. It reads the current repository, invokes explicitly requested local commands, and writes redacted mode-`0600` logs under a private sandbox-safe runtime directory, normally `/tmp/agentq-<uid>/<repository-hash>/`.

`~/.agents/agentq/scripts/install-tools.sh --apply` is the only bundled script that intentionally contacts package registries. Review its printed plan before applying it.
