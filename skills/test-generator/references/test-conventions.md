# Test conventions

Used by `test-writer`. If the project already has tests, its framework and libraries decide the syntax (from the scan); these are the defaults for style and quality.

## Defaults (new test projects)

- Framework: xUnit via `xunit.v3.mtp-off` (classic `dotnet test` works on the .NET 9 and 10 SDKs); when the project already runs on Microsoft.Testing.Platform, a new test project follows the profile the scan chose (`mtp-xunitv3` only when `global.json` selects MTP on SDK 10), and an existing test project keeps its own stack. Assertions: the built-in `Assert`. Mocking: NSubstitute, only when a dependency boundary must be faked.
- Location: `tests/<Project>.Tests/`, one test class per production class, mirroring namespaces. File name `<Class>Tests.cs`.
- Names: `Member_Condition_ExpectedResult`, e.g. `Apply_PriceExactly100_AppliesBulkDiscount`.
- Structure: Arrange / Act / Assert, one behaviour per test. Use `[Theory]` + `[InlineData]` only when every row shares one arrangement and one assertion and differs in data.
- Traceability: put a comment `// S014` naming the scenario id on the line directly above the test method (or above the row).

## Assertions

- Assert the whole outcome: exact value, exact exception type (`Assert.Throws<T>`, and `ParamName`/message when the scenario states it), state change, and collaborator calls only when the call *is* the behaviour.
- Decimal and floating point: assert with the precision the spec states; compare `double` with a tolerance; never round the actual value to make it match.

## Pitfalls seen in real runs

- **NSubstitute and expression parameters:** `Arg.Is<Expression<Func<...>>>(...)` often fails to compile (CS0121, ambiguous overload). Capture the argument instead (`Arg.Do<Expression<Func<...>>>(e => captured = e)` or `ReceivedCalls()`) and assert on what was captured.
- **No echo tests:** do not assert only that a mock returns what the test itself told it to return, and do not assert only `NotNull` on a captured argument; assert the value that makes the scenario true.
- **A database base class only where the database is used:** do not make every test class create a connection and a schema; derive from the database fixture only in the classes whose scenarios run EF queries.
- **Do not test a neighbour through the target:** validator rules, mapper configuration and DTO shape are tests of those classes; test them through the target only when a scenario says the target depends on that behaviour.
- **Every async test gets a `CancellationTokenSource` with a timeout.**
- Arrange / Act / Assert stay visible in every test (blank lines or comments), in the same style within one test class.

## Isolation and determinism

- Real objects for pure logic. Fake only true boundaries (I/O, HTTP, clock, randomness, database, e-mail).
- Never mock the class under test or simple value objects.
- Fixed clock (`TimeProvider`/fake), fixed random seed, no `Thread.Sleep`, no real network, no dependence on test order, no shared mutable statics.
- Culture-sensitive scenarios set the culture explicitly inside the test and restore it.
- Async tests return `Task`; never `async void`.

## Database tests

See `ef-and-db.md` (options) and `db-fixture.md` (real database in Docker).

## Forbidden (the lint blocks these)

`Assert.True(true)`, tests without assertions, assertion of only `NotNull`/`NotEmpty`, empty `catch`, skipped tests, comparing a value with itself, copying production logic into the test to compute the expectation.
