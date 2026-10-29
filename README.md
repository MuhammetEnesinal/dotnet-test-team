# dotnet-test-team

A [Claude Code](https://code.claude.com) plugin that writes **and runs** unit tests for C# code. It derives the test scenarios from the code, shows them to you, and writes tests only after you approve. Tests use your project's own test stack.

> [!WARNING]
> - Building and testing **run your project's code** with your user privileges. Use it on code you trust.
> - It uses Claude model usage (about 0.5 to 6 USD per request, see [Cost](#cost-and-models)).
> - Status 0.7.0: tested on Windows. **macOS and bare-metal Linux are untested**, see [What was verified](#what-was-verified).

Contents: [Quick start](#quick-start) · [Requirements](#requirements) · [Install](#install) · [Use](#use) · [Real database](#real-database-in-docker) · [Cost](#cost-and-models) · [Safety and limits](#safety-and-limits) · [Verified](#what-was-verified) · [Troubleshooting](#troubleshooting)

## Quick start

```
cd /path/to/your-csharp-repository
claude --plugin-dir /path/to/dotnet-test-team
> Write unit tests for src/Orders/DiscountCalculator.cs
```

Claude restates what it will do and asks for your consent, shows a table of scenarios, and waits for your approval. Only then it writes, runs and reports.

## What it does and does not do

| It does | It does not |
|---|---|
| Lists scenarios with `file:line`, expected value and the source of that value | Take expected values from running your code |
| Asks for your approval before writing any test | Write tests before you approve (an instruction to the model, see [Safety](#safety-and-limits)) |
| Writes new test files (xUnit, NUnit, MSTest; your assertion and mocking libraries) | Edit production code, your existing tests or your project files |
| Builds, runs, repeats a green run twice to catch flaky tests, repairs its own test mistakes (3 rounds) | Hide a failing test: a test that exposes a bug is kept and reported as a *possible code bug* |
| For EF Core code: offers a real throw-away database of your project's kind in Docker | Use the InMemory provider or mock `DbSet`, unless you choose that yourself |
| Follows your wishes: number of tests, own parameters and expected values, focus area, one parameterized test, several targets | Measure coverage or run mutation testing |

Supported code: C# on .NET 8 or newer; classes, services, helpers, controllers (the action is called directly; filters, routing and model binding are not covered), validators (a real validator instance). Refused or asked first: Razor/XAML, generated code, interfaces alone, minimal APIs in `Program.cs`, WinForms/WPF.

### Supported test stacks

| Stack | Classic `dotnet test` (VSTest) | Microsoft.Testing.Platform (MTP, .NET SDK 10 only) |
|---|---|---|
| xUnit v2 / v3 | supported | supported, verified |
| NUnit | supported | supported, verified |
| MSTest (also `MSTest.Sdk`) | supported | supported, verified |
| TUnit | not supported | not supported |

An MTP project needs `Microsoft.Testing.Extensions.TrxReport`; the plugin never adds packages to an existing project.

## Requirements

| Needed | Minimum | Tested with |
|---|---|---|
| Claude Code | 2.1.271 | 2.1.30x, terminal (desktop and IDE: same plugin system, not tried here) |
| `bash` | any | Git Bash from [Git for Windows](https://git-scm.com/download/win) on Windows |
| Python (standard library only, nothing to install) | 3.9 (end-of-life upstream; prefer a current version) | 3.14 on Windows; 3.9 and 3.13 in Linux containers (helper test suite) |
| .NET SDK | 8 (MTP projects: 10) | 9.0.308, 10.0.402 |
| Docker with Linux containers | optional, only for a real test database | Docker Desktop 29.x |
| Claude account | one that can use Claude Code (Pro, Max, Team, Enterprise or API) | Pro |

Start Claude Code in the root folder of your repository. claude.ai on the web and Cowork: not checked.

**Windows needs both Git for Windows and Python 3.9+ on `PATH`.** Without `bash` the hooks cannot start (simulated: exit 9009; with only the WSL `bash.exe`: exit 1). Claude Code treats that as non-blocking, so the write guard silently does not run and nothing warns you; the rest of the plugin still needs `bash` for every `dtt` call.

## Install

The plugin is a folder. The commands are the same on Windows, macOS and Linux.

**Try it for one session:**

```
git clone https://github.com/MuhammetEnesinal/dotnet-test-team.git      # or unzip it anywhere
claude --plugin-dir /path/to/dotnet-test-team
```

**Install persistently** (the folder contains `.claude-plugin/marketplace.json`):

```
claude plugin marketplace add MuhammetEnesinal/dotnet-test-team   # or: add /path/to/dotnet-test-team
claude plugin install dotnet-test-team@dotnet-test-team
```

The local-folder form was tried on Windows: install, `claude plugin list`, `claude plugin details` (1 skill, 3 agents, 2 hooks) and uninstall all worked. Installing from GitHub (`MuhammetEnesinal/dotnet-test-team`) also worked on Windows: the marketplace was added, the plugin installed (1 skill, 3 agents, 2 hooks) and its launcher ran from the install cache.

**Check:** type `/` and look for `dotnet-test-team:test-generator`, or run `claude plugin list`. `bash /path/to/dotnet-test-team/bin/dtt --platform` prints what the plugin sees (Python, OS), useful in bug reports. `claude plugin validate /path/to/dotnet-test-team` checks the manifest.

**Linux:** install Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`), check `python3 --version` (3.9 or newer) and `dotnet --version`, then use either form above.

**Update:** replace the folder (or `git pull`) and restart Claude Code. **Uninstall:** `claude plugin uninstall dotnet-test-team@dotnet-test-team` (or stop passing `--plugin-dir`) and delete the folder. What stays behind: `.test-generator/` in your projects, kept test-database containers (`dtt db down --project-root <root> --engine <engine> --kept`), and Docker images the plugin downloaded (the report prints the `docker rmi` line).

## Use

Say what you want in a normal sentence, or call the skill:

```
Write unit tests for src/Orders/DiscountCalculator.cs
/dotnet-test-team:test-generator src/Orders/DiscountCalculator.cs only the boundary at 100, three tests
/dotnet-test-team:test-generator OrderService.Cancel use these rows: price 100 Gold -> 80; price 50 Silver -> 50
/dotnet-test-team:test-generator --resume
```

| Option | Meaning |
|---|---|
| `--mode standard` (default) | The scenario analyst runs on Sonnet. |
| `--mode deep` | The analyst runs on Opus: more scenarios, about twice the analysis cost, see [Cost](#cost-and-models). |
| `--lang <code>` | Language of the chat. Created files are always English. |
| `--resume` | Continue an unfinished run, for example after a usage limit. |

### How a run works

1. **Consent.** The skill restates what it understood and says that building and testing run your project's code. The skill is instructed to run nothing before your yes.
2. **Scan** (reads files, starts no process): test projects and stack, the database engine and its version from `docker-compose`, model features a stand-in database cannot imitate (row versions, filtered indexes, check constraints), build-time code, NuGet sources.
3. **Analyze.** `scenario-analyst` lists scenarios: id, input, expected value, and where the expected value comes from. Unclear cases become questions, not guesses.
4. **You approve**, edit, drop, or answer. Nothing is written before this.
5. **Database decision** (EF Core code only): a real database of your kind in Docker (recommended), SQLite in memory, a small hand-written helper, or mocks only, each with what it cannot imitate.
6. **Write.** `test-writer` writes new test files; the skill asks for at most 40 scenarios per batch. A test project is created only if none exists.
7. **Run.** `dtt run` lints the new files, builds, runs, repeats a green run twice, and prints at most 15 lines. At most 3 rounds per request; the model repairs its own test mistakes between rounds.
8. **Report.** Passed, failed, flaky, `production: unchanged`. Failures that point at the code are *possible code bugs* with input, expected, actual and `file:line`. At the end you are asked whether to delete a test-database container or keep it.

An illustrative session (made-up example values, not a recorded run):

```
you:     Write unit tests for src/Pricing/DiscountCalculator.cs, only the boundary at 100, 3 tests
claude:  I will scan the project, show scenarios, and write tests only after you approve.
         Building and testing run your project's code. Continue? (yes / no)
you:     yes
claude:  S001  Apply(100m, Gold)   -> 80    (rule: Gold = 20% off; DiscountCalculator.cs:14)
         S002  Apply(99.99m, Gold) -> 79.99 (no discount below 100; DiscountCalculator.cs:11)
         S003  Apply(100m, None)   -> 100   (DiscountCalculator.cs:9)
         Approve these 3? (all / edit / drop)
you:     all
claude:  Written 3 tests.
         run 20261028-101500-discountcalculator round 1/3: passed=2 failed=1 flaky=0 skipped=0
         production: unchanged
         Possible code bug S002: expected 79.99, actual 80 (DiscountCalculator.cs:11).
```

### What it writes to disk

| Where | What | When removed |
|---|---|---|
| `.test-generator/` in your project (own `.gitignore`) | per run: `state.json`, scan result, `01-scenarios.json` (includes your request text), your decisions, per-round notes, `written-files.txt`, `trx/`, `snapshot/`, `db.json` (only with a database) | When a run is `done`, only `state.json`, `04-findings.md`, `report.md` and `memory.md` stay. Delete the folder any time. |
| New test files, and a new test project only if none exists | tests in your stack; packages from the fixed list in `data/packages.json` | Yours to keep or delete. No package is added to an existing project without your agreement. |
| A Docker container (only if you chose a real database) | the test database | Deleted at the end unless you chose to keep it. |

Production code, existing tests, `global.json` and project files are not edited. **What leaves your machine:** the plugin opens no network connection itself. Claude Code sends the conversation, including the code it reads, to Anthropic as usual. `dotnet` may download NuGet packages; `docker` may pull a database image, only after your yes.

## Real database in Docker

For EF Core code the recommended answer is a throw-away container of your project's own database engine. `dtt db` starts it from a fixed table of official images (SQL Server, PostgreSQL, MySQL, MariaDB, Oracle). The engine comes from your project; the image tag from your `docker-compose` file when its repository is in that table.

- The image is downloaded only after your yes. **SQL Server and Oracle images carry licence terms** (Microsoft's EULA, Oracle's Free Use Terms): the skill asks you before any download or container start. For SQL Server the helper also needs your recorded yes (`--accept-eula`); for Oracle the helper has no such flag, so only the skill's question protects you. SQL Server starts in the free Developer edition.
- Port on `127.0.0.1` only, random password, memory limit, no bind mounts; the data volume is removed with the container. The tests get the connection only through the environment variable `DTT_TEST_DB_CONNECTION`, and each test class creates and drops its own database.
- The container is deleted at the end unless you keep it for the next runs (a kept SQL Server holds about 2 GB RAM). Leftovers of crashed runs (`dtt-run-...`, label `dtt.plugin=1`) are removed at the next session start.
- SQL Server images are x86-64 only (Apple Silicon: unsupported). Without Docker the plugin says why and offers the other options.

## Cost and models

The skill and the three agents ask for **Sonnet**; `--mode deep` runs the analyst on Opus.

- A skill's `model:` setting lasts for the current turn only; your session model returns at your next message. The agents keep their own `model:` line. For a predictable cost start the session on Sonnet (`/model sonnet`); in one measured run 86% of the cost was the main conversation running on Opus.
- Idle cost: about 540 tokens per session while the plugin is enabled (`claude plugin details dotnet-test-team@dotnet-test-team`). The skill text (about 5,000 tokens) loads when the skill runs; each agent loads 850 to 2,300 tokens when it starts.

**Measured on real runs** (Windows, Claude Code 2.1.x, Sonnet, `--mode standard` unless noted; October 2026). Dollar figures are Claude Code's own list-price estimates; on Pro or Max they are plan allowance used, not money charged. One request has several steps: scan, analysis, write-and-run (repeated after a question or a failed attempt).

| Target | Scenarios / tests | Steps (scan + analysis + write-and-run), USD | Total |
|---|---|---|---|
| One small class | 3 / 3 | complete run | about 0.6 |
| `TokenService` (JWT, no database) | 12 / 6 written, 6 report-only | 0.08 + 0.25 + 0.51 | about 0.8 |
| `CustomerService` (EF Core, SQL Server in Docker) | 14 / 14, all green | 0.08 + 0.32 + 1.43 (0.36 of it a first attempt that stopped because Docker was not running) | about 1.8 |
| `PurchaseOrderService` (EF Core, SQL Server in Docker) | 50 / 49; 48 passed, 1 possible code bug; 49 passed after the bug was fixed in a copy | 0.08 + 0.34 + 1.13 + 1.87 + 2.50 (the 1.87 step ran on SQLite, failed, and was redone on SQL Server) | about 5.9 |
| `OrderQueries` (EF Core, PostgreSQL in Docker) | 9 / 11, all green | 0.08 + 0.23 + 0.49 | about 0.8 |
| `OrderQueries` (EF Core, MariaDB in Docker) | 8 / 10, all green | 0.08 + 0.21 + 0.50 | about 0.8 |
| `OrderQueries` (EF Core, MySQL in Docker) | 8 / 10, all green | 0.08 + 0.22 + 0.50 | about 0.8 |
| `OrderQueries` (EF Core, Oracle in Docker) | 9 / 10; 8 passed, 2 reported as possible code bugs | 0.08 + 0.21 + 0.54 | about 0.8 |

Treat this as a range of roughly **0.5 to 6 USD per request**, not a promise: eight targets are too few to predict your project. Drivers: code size, scenarios, rounds, database setup, whether the main session is on Opus, prompt-cache state. Check `/usage` for your own numbers; prices are on Anthropic's published API pricing page. To change models, edit the `model:` line in `skills/test-generator/SKILL.md` and `agents/*.md` (keep your own copy of the folder; edits in a plugin cache are overwritten on update).

**`standard` and `deep` on the same targets and approval answers** (one run each, so indicative only):

| Target | Mode | Scenarios | Tests written / green | Analysis cost, USD | Total, USD |
|---|---|---|---|---|---|
| `TokenService` | standard | 12 | 6 / 6 | 0.25 | about 0.8 |
| `TokenService` | deep | 16 | 11 / 11 | 0.59 | about 1.5 |
| `CustomerService` | standard | 14 | 14 / 14 | 0.32 | about 1.8 |
| `CustomerService` | deep | 37 | 37 / 37 | 0.76 | about 3.3 |

`deep` finds more scenarios (more boundary cases such as field lengths, fewer cases left unclear) at about two to two and a half times the analysis cost. On these two targets it found no code bug that `standard` missed. Start with `standard`; use `deep` for critical classes or when `standard` leaves many cases open.

## Safety and limits

**Enforced in code:** where the subagents may write (the writer only in the run's test project, new files and files it created; the analyst only the run's `01-scenarios.json`); script-owned run files; path confinement (real-path checks, links not followed); limits on JSON/XML read from the project; scenario-file validation; the `production:` check after each run; the round counter in `dtt run`; a run with no passing test is never green and a non-zero `dotnet test` exit code never gives a green result; trx and `*.log` files are redacted or deleted on every exit path; the helper uses no `shell=True` and no `eval`.

**Instructions to the model, not technical locks:** asking for your approval before writing; asking consent before any `dotnet` command, image download or container start; not changing production code; keeping expected values out of running code; never weakening a test; asking before the 4th round. A model that ignores the skill could break these. In Claude Code's `auto`, `acceptEdits` and `bypassPermissions` modes Claude Code adds little or no prompt of its own, so the skill's question is then the **only** approval gate. For a real stop, use the default permission mode. Claude Code's own rule: permission rules are enforced by the tool, instructions to the model are not.

**Permissions.** The plugin pre-approves nothing. The skill calls the helper as `bash <plugin>/bin/dtt ...` with the path **unquoted**, which is the only form Claude Code's permission rules match. One narrow rule is enough: `Bash(bash *dotnet-test-team*/bin/dtt *)`. Do not approve broad rules like `bash *`. That rule also covers `dtt db up --allow-pull --accept-eula`, so a download or licence acceptance is guarded by the skill's question and your answer, not by a lock.

**Hooks (2):**

| Event | Hook | Does |
|---|---|---|
| `SessionStart` | `session-resume` | Prints a note when unfinished runs exist. If a run folder records a test-database container, asks Docker (at most 5 s, fails open) to remove leftover plugin containers of finished or old runs. |
| `PreToolUse` Write, Edit, MultiEdit, NotebookEdit | `guard-writes` | For the plugin's subagents only: confines writes (see above) and records the test files the writer wrote. Nobody may write script-owned run files with the Write tools. |

A hook that cannot start is non-blocking in Claude Code, so the guards fail open. The launcher exits 2 for a hook only when a `.test-generator/runs` folder exists and no usable Python is found.

**Limits:**

- The guard covers the plugin's subagents, not the main conversation, and is not a sandbox: `dotnet build` and `dotnet test` run your project's code. Production edits by the main conversation show up only in the `production:` line of `dtt run`.
- MTP runs on .NET SDK 10 with the `global.json` runner key only; MTP on SDK 9 is refused. A test project with several target frameworks, or a `;` in its path, is refused. The plugin never edits your `global.json`, project files or packages to make a combination run. MTP detection reads the project file only, so a setting in `Directory.Build.props` or a `Condition` may be missed; that ends in a refusal or "not trusted", never in a green result.
- Your request text is stored by the analyst, a model, in `01-scenarios.json`; the code cannot prove it is verbatim. A `.test-generator` folder planted in a repository is untrusted data; resuming needs your confirmation. `update-state` and `run` calls are not locked; the skill calls them one at a time.
- Database images are pinned by tag, not by digest. The Docker container sits on the default bridge network and its password is visible to anyone who can run `docker inspect`. The lint rule against literal connection strings in tests is a help, not a boundary.
- Coverage is not measured and there is no mutation testing. When a usage limit is hit, state is on disk and `--resume` continues.

## What was verified

Everything below was run by the author; the test material is not part of this repository. Nothing else is claimed.

| Check | Result |
|---|---|
| Real model runs on a copy of a multi-project ASP.NET / EF Core solution (the original was only read) | `TokenService`, `CustomerService`, `PurchaseOrderService`: all tests green on Windows (one real code bug found in `PurchaseOrderService`); the finished tests also ran green on Linux |
| Microsoft.Testing.Platform, .NET SDK 10.0.402 | xUnit v3, NUnit, MSTest: counts equal `dotnet test`; xUnit v3 also on Linux; one model run end to end on Windows |
| `standard` and `deep` | Both run on the same real targets, see [Cost](#cost-and-models) |
| Database engines | SQL Server: model runs on Windows, `dtt run` on Linux. PostgreSQL 16 and MariaDB 11: one model run each on Windows, green, container removed. MySQL 8: one model run, 10/10 green. Oracle Free 23 (`gvenzl/oracle-free:23-slim`, 2.84 GB on disk, ready in about 45 s after the pull): one model run, 8 of 10 tests passed and 2 failed because the Oracle provider throws `OracleException` instead of `OperationCanceledException` for an already-cancelled token; the plugin kept them and reported a possible code bug (or a wrong expectation for Oracle) instead of weakening them |
| Refusals on a real SDK | Multi-target project, MTP on SDK 9, MTP on SDK 10 without the `global.json` key, a `;` in the path: refused before any build |
| Platforms | Windows 11 x64 native. Linux: a Linux container (.NET SDK, Python, Docker CLI) on a Windows Docker Desktop host; the model runs happened on Windows, on Linux the helper `dtt run` ran on the finished tests. **macOS: untested. Linux on a bare-metal host: untested.** Windows without Git Bash: simulated, see Requirements and Troubleshooting |
| Helper program | An automated suite of 928 tests passes on Windows (78 skipped) and in Linux containers with Python 3.9 and 3.13 (76 skipped; skips need Windows-only features, the .NET SDK or Docker) |
| Install from a local marketplace folder and from GitHub | Worked on Windows (add, install, details, launcher run, uninstall) |

## Troubleshooting

| Problem | What to do |
|---|---|
| The skill does not show up | Start Claude Code with `--plugin-dir` pointing at the folder that contains `.claude-plugin/`. Run `claude plugin validate <folder>`; look at the `/plugin` Errors tab. |
| "no usable Python" or a hook error at start | Install Python 3.9+ on an absolute `PATH` entry (the Windows Store `python3` stub is skipped on purpose). Run `bash <plugin>/bin/dtt --platform`. |
| On Windows nothing runs, or the guard seems absent | Install Git for Windows and Python 3.9+; the hooks and launcher use `bash`. Without it the hooks fail open. |
| The launcher blocks a hook (exit 2) | Only when `.test-generator/runs` exists and Python is missing. Install Python or delete that folder. |
| `dtt run` says "not trusted" | `dotnet test` failed without a failing test (restore, SDK or environment). Fix the environment and run again; the round is consumed. |
| Database tests fail with SQLite errors such as `no such function: LEN` | The project uses features of its real engine that SQLite cannot imitate. Choose the real-database option. |
| "Docker is not available" | Start Docker (Linux containers). Without it the plugin offers SQLite, a helper or mocks. |
| Leftover test containers | `bash <plugin>/bin/dtt db sweep`, or `docker ps -a --filter label=dtt.plugin=1`. |
| A usage limit stopped the run | Start again with `--resume`. |
| Hooks seem to do nothing | Check `claude --debug` output (files in `~/.claude/debug/`). |

To report a problem, include `bash <plugin>/bin/dtt --platform`, the Claude Code version and the last lines of the `dtt run` output at https://github.com/MuhammetEnesinal/dotnet-test-team/issues .

## License

[MIT](LICENSE), copyright MuhammetEnesinal.
