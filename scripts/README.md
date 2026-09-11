# Codex lane manager command reference

`scripts/codex-lanes` manages the three isolated Codex worktrees currently
configured for this repository. FinPay Top-Up and Telegram remain separate
ownership lanes but do not have dedicated launcher worktrees yet.

| Lane | Default branch | Default checkout |
|---|---|---|
| Integrator | `integration/mmpp-next` | repository root |
| FinPay | `agent/finpay-next` | sibling `kestra-mmpp-pipeline-finpay` |
| LinkAja | `agent/linkaja-next` | sibling `kestra-mmpp-pipeline-linkaja` |

Run the launcher from the integration checkout. It creates Git worktrees and a
tmux session, but it never merges branches, cherry-picks commits, deploys
workflows, runs migrations, deletes worktrees, stages files, or cleans local
changes.

## Requirements

- Bash
- Git with worktree support
- `flock`
- tmux for `start` and `stop`
- Codex CLI for `start`
- A clean `integration/mmpp-next` checkout for `init` and `start`

Show the built-in summary with:

```bash
scripts/codex-lanes --help
```

## Commands

### `init`

```bash
scripts/codex-lanes init
```

Creates the FinPay and LinkAja branches and sibling worktrees from the
integration branch. It refuses to run when:

- the integration checkout is detached or on a different branch;
- the integration worktree is dirty, including untracked files;
- either domain branch already exists; or
- either target worktree path already exists.

Ignored local files such as `.env` and `data/` are not copied into the new
worktrees.

`init` is normally run once. Existing lanes should be inspected with `status`,
not initialized again.

### `start`

Choose one LinkAja context option when using `start`.

#### Continue a known LinkAja conversation

```bash
scripts/codex-lanes start --linkaja-fork <SESSION_ID>
```

`SESSION_ID` must be a UUID. Codex forks that conversation into the LinkAja
worktree, preserving the original conversation while creating an isolated
continuation.

#### Select a LinkAja conversation interactively

```bash
scripts/codex-lanes start --linkaja-picker
```

The LinkAja window runs `codex fork --all`, allowing selection from sessions
created in other working directories. After selecting the conversation, tell
it to continue on `agent/linkaja-next` using
`linkaja_fee_pipeline/docs/plans/reversal-close.md` as the durable
plan.

#### Start LinkAja without previous chat context

```bash
scripts/codex-lanes start --linkaja-fresh
```

This is the explicit opt-out from LinkAja conversation preservation.

For every mode, the integrator and FinPay windows start as new top-level Codex
sessions with their checked-in prompts. The launcher then creates this tmux
layout:

| Window | Working directory | Startup prompt or mode |
|---|---|---|
| `integrator` | integration checkout | `.codex/prompts/integrator.md` |
| `finpay` | FinPay worktree | `.codex/prompts/finpay.md` |
| `linkaja` | LinkAja worktree | fork, picker, or `.codex/prompts/linkaja-relocation.md` |

Every Codex TUI uses `--strict-config` and `--no-alt-screen`. The managed tmux
session enables mouse support, uses a 50,000-line history by default, and opens
on the `integrator` window.

Before starting, the command verifies that:

- all three worktrees exist on their expected branches;
- all three worktrees are clean;
- tmux and Codex are available; and
- the configured tmux session does not already exist.

Attach after startup:

```bash
tmux attach -t mmpp-agents
```

Useful tmux keys:

| Action | Key |
|---|---|
| Next window | `Ctrl-b n` |
| Previous window | `Ctrl-b p` |
| Choose a window | `Ctrl-b w` |
| Enter keyboard scroll/copy mode | `Ctrl-b [` |
| Exit copy mode | `q` |
| Detach without stopping agents | `Ctrl-b d` |

With mouse mode enabled, the wheel scrolls tmux history. Detaching keeps all
agents running; `stop` terminates them.

### `status`

```bash
scripts/codex-lanes status
```

Displays each lane's checkout path, branch, short commit, and clean/dirty state.
For a dirty lane, it also prints `git status --short`. The last line reports
whether the configured tmux session is running.

`status` is read-only and is the recommended command before starting agents,
requesting a handoff, or integrating a domain branch.

### `check`

```bash
scripts/codex-lanes check finpay
scripts/codex-lanes check linkaja
```

Checks committed branch changes, staged changes, unstaged changes, and
untracked files against the lane allowlist. It exits unsuccessfully and prints
each `out-of-lane` path when a domain agent changed an integrator-owned or
other-domain file.

The allowlists are:

| Lane | Allowed paths |
|---|---|
| FinPay reporting | `finpay_pipeline/**`, related reporting tests |
| FinPay Top-Up | `finpay_topup_pipeline/**`, related Top-Up tests |
| Telegram | `services/telegram_bot/**`, related bot tests |
| LinkAja | `linkaja_fee_pipeline/**`, related LinkAja tests |

Run the relevant check before asking the integrator to review or integrate a
domain branch. Passing this check verifies file ownership only; it does not run
tests or prove that a change is production-safe.

### `stop`

```bash
scripts/codex-lanes stop
```

Kills only the configured tmux session. This terminates the Codex processes in
its windows but preserves branches, worktrees, commits, and working files. It
is safe to call when the managed session is already stopped.

### `help`, `--help`, and `-h`

```bash
scripts/codex-lanes help
scripts/codex-lanes --help
scripts/codex-lanes -h
```

Print the short command summary.

## Environment overrides

Set overrides for one invocation by placing them before the command:

```bash
CODEX_LANES_TMUX_HISTORY_LIMIT=100000 \
  scripts/codex-lanes start --linkaja-picker
```

| Variable | Default | Purpose |
|---|---|---|
| `CODEX_LANES_INTEGRATION_BRANCH` | `integration/mmpp-next` | Expected integration branch and base for lane checks |
| `CODEX_LANES_FINPAY_BRANCH` | `agent/finpay-next` | FinPay worktree branch |
| `CODEX_LANES_LINKAJA_BRANCH` | `agent/linkaja-next` | LinkAja worktree branch |
| `CODEX_LANES_FINPAY_PATH` | sibling `<repo>-finpay` | FinPay worktree path |
| `CODEX_LANES_LINKAJA_PATH` | sibling `<repo>-linkaja` | LinkAja worktree path |
| `CODEX_LANES_TMUX_SESSION` | `mmpp-agents` | Managed tmux session name |
| `CODEX_LANES_TMUX_HISTORY_LIMIT` | `50000` | History lines for each newly created pane; must be a positive integer |
| `CODEX_LANES_CODEX_BIN` | `codex` | Codex executable name or path |

Overrides change what the current invocation expects; they do not rename an
existing branch, move an existing worktree, or modify global Git/tmux/Codex
configuration.

## Recommended lifecycle

First-time setup:

```bash
scripts/codex-lanes init
scripts/codex-lanes status
scripts/codex-lanes start --linkaja-picker
tmux attach -t mmpp-agents
```

Domain handoff:

```bash
scripts/codex-lanes status
scripts/codex-lanes check finpay
scripts/codex-lanes check linkaja
```

Restart while retaining a known LinkAja conversation:

```bash
scripts/codex-lanes status
scripts/codex-lanes stop
scripts/codex-lanes start --linkaja-fork <SESSION_ID>
tmux attach -t mmpp-agents
```

All worktrees must be clean before `start`. Ask each domain agent to finish its
checkpoint and commit its lane before restarting. Never run the same saved
conversation concurrently in multiple checkouts.

## Internal function map

These are implementation details for maintainers, not additional shell
commands:

| Function | Responsibility |
|---|---|
| `usage` | Prints the short public command reference |
| `die` | Prints an error and exits unsuccessfully |
| `require_command` | Verifies an executable is available |
| `repo_root` | Resolves the current Git repository root |
| `canonical_dir` | Normalizes a directory to its physical absolute path |
| `current_branch` | Reads the checked-out branch for a worktree |
| `require_integration_checkout` | Enforces the configured integration branch |
| `require_clean_worktree` | Rejects staged, unstaged, or untracked changes |
| `acquire_lock` | Prevents concurrent launcher mutations using the shared Git directory |
| `branch_exists` | Tests whether a local branch exists |
| `assert_path_absent` | Protects an existing path from worktree creation |
| `assert_lane_worktree` | Verifies a lane path, worktree root, and branch |
| `init_lanes` | Creates the two domain branches and worktrees |
| `prompt_text` | Loads a required checked-in startup prompt |
| `start_lanes` | Validates state and creates the configured tmux/Codex session |
| `lane_status` | Formats one lane's Git state |
| `show_status` | Reports all lanes and the managed tmux session |
| `allowed_path` | Encodes the FinPay and LinkAja file allowlists |
| `check_lane` | Compares all lane changes with the appropriate allowlist |
| `stop_lanes` | Stops only the managed tmux session |

Startup installs an error/signal trap after creating the temporary tmux
bootstrap window. If later window creation fails, the trap removes the partial
managed session. It does not alter any Git worktree.
