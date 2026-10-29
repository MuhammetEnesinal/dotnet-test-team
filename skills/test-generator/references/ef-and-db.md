# EF Core and a test database

Applies only when the scan shows EF Core and an approved scenario runs EF queries (`FirstOrDefaultAsync`, `Include`, `Where` on a `DbSet`, `SaveChanges`). Pure logic and non-query behaviour use a fake of the repository interface instead.

**Never** use the `Microsoft.EntityFrameworkCore.InMemory` provider (it ignores relational behaviour) and never mock `DbSet`/`IQueryable` for queries, unless the user explicitly chooses that. Never add a package silently. A stand-in database differs from the production one (RowVersion, decimal ordering, filtered indexes, check constraints, column types), so the best test database is **the same kind as the project's own**.

## Ask once, record the answer in `02-approval.md`

The scan's `db:` line gives the engine, the tag (from the project's own compose file or the plugin default), whether migrations exist and which model features a stand-in cannot imitate (row versions, filtered indexes, check constraints...). Show those features to the user in one line.

1. **A real database of the project's own kind in Docker (recommended when `dtt db check` says `docker: ok`).** The plugin starts a throw-away container of the project's engine, gives the tests its connection through an environment variable, and removes it afterwards (the user decides at the end: delete or keep for the next runs). Say honestly: needs Docker with Linux containers running; the first run may download the image (the check prints the size, ask before `--allow-pull`); SQL Server needs about 2 GB of RAM for Docker and its licence acceptance (give the link the check prints, ask before `--accept-eula`); the tests are integration tests and slower than SQLite. If the check says unavailable, show its reason in plain words and mark this option unavailable.
2. **SQLite in memory** (`Microsoft.EntityFrameworkCore.Sqlite`, exactly the project's own EF Core version): close, not identical. Provider-specific mappings and features can fail. For a **new** test project pass `--ef-sqlite-version <version>` to the scaffolder; for an **existing** one show `dotnet add <test csproj> package Microsoft.EntityFrameworkCore.Sqlite --version <version>` and run it only after the user agrees (central package management: the user adds the version themselves). Name the features from the scan line that SQLite cannot test.
3. **A small hand-written async query helper** inside the test project (no package): fine for simple lookups, does not imitate sorting, null comparison, `Include`, query translation. Mark such scenarios `fidelity: low` in the report.
4. **Mocks only:** scenarios that need a real query stay open questions.

A project that already uses SQLite as its database needs no question: use SQLite. A project with an engine that has no recipe (`recipe: false` in the scan line) cannot use option 1: say so.

## Environment problems are not test bugs

A login failure, a refused connection, `NOT NULL constraint failed ... RowVersion` or `SQLite does not support ...` comes from the database environment, not from the test or the code. `dtt run` prints it as `environment` and does not count a repair round. **Never patch a provider difference silently** (for example a test-only context that fakes a row version): stop, tell the user what the stand-in cannot do, and offer the real database. If the user still chooses a stand-in, list in the report what stays untested (concurrency checks, filtered indexes, decimal ordering...).

## How to write the tests

- Real database (option 1): follow `db-fixture.md`.
- SQLite (option 2): one open `SqliteConnection("DataSource=:memory:")` per test; `UseSqlite(connection)`; `EnsureCreated()`; **arrange** with one context, **act** with a fresh context on the same connection, **assert** with a third if needed (a shared context serves tracked entities from memory and hides bugs); seed only what the scenario needs with explicit keys. If `EnsureCreated()` or a query fails because of a provider-specific mapping, stop and report the mapping; do not rewrite the test to hide it.
