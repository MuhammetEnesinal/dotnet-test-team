# Database fixture for a real throw-away database

Used when the user chose a real database in Docker (`dtt db up` started it). `dtt run` gives the tests the environment variables `DTT_TEST_DB_CONNECTION` (admin connection to the server: `sa`, `postgres`, `root` or `system`) and `DTT_TEST_DB_ENGINE` (`sqlserver`, `postgres`, `mysql`, `mariadb`, `oracle`). **These are the only way a test learns where the database is.** A test never contains a host, a user or a password as a literal (`dtt run` lints this), never opens any other server and never starts a process.

## One shared fixture file per test project

Write one new file (for example `Support/DbFixture.cs`), reuse it for every test class of the run. Use the provider package the project under test already references (it comes with the project reference); if the provider package is not visible to the test project, that is a `problem` for the orchestrator, never a package you add.

The fixture does, per test class (one fixture instance per class: `IClassFixture<>` in xUnit, `[OneTimeSetUp]` in NUnit, `[ClassInitialize]` in MSTest):

1. Read `DTT_TEST_DB_CONNECTION`. **If it is missing or empty, skip the tests with the message** `Set DTT_TEST_DB_CONNECTION to a database server you may use, or run the tests through dtt run.` (xUnit v3 `Assert.Skip`, NUnit `Assert.Ignore`, MSTest `Assert.Inconclusive`; xUnit v2 has no runtime skip, so throw an exception with exactly that message). Never let it end as a connection error, and never let it pass silently.
2. Build a connection string for a **new database named `dtt_<guid without dashes>`** from the admin connection with the provider's own connection-string builder (`SqlConnectionStringBuilder`, `NpgsqlConnectionStringBuilder`, `MySqlConnectionStringBuilder`, `OracleConnectionStringBuilder`): copy the admin string, set only the database/catalog name.
3. Create the schema with the project's own `DbContext` class (its options constructor; add the same interceptors the production registration adds): `context.Database.Migrate()` when the scan says migrations exist, otherwise `EnsureCreated()`. Oracle has no per-test databases: create a user/schema instead, or ask the orchestrator.
4. Hand tests a way to create a **fresh context on the same database** for arrange / act / assert, as in the SQLite recipe.
5. On dispose drop the database (`context.Database.EnsureDeleted()` or the provider's `DROP DATABASE` through the admin connection). The container is removed by the plugin anyway; dropping keeps parallel classes and repeated passes clean.

Seed only what the scenario needs with explicit keys. Tests that check concurrency (row version) save a change through context B after context A loaded the same row, then expect `DbUpdateConcurrencyException`; this works only on a real database.

## Do not

- Do not catch and hide provider errors. A failing migration or a failing query is reported (the orchestrator treats it as an environment line or a finding).
- Do not set `DOTNET`/environment variables in code, do not write the connection string anywhere, do not log it.
- Do not use the EF InMemory provider or SQLite in this fixture.
