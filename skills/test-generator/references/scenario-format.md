# Scenario file format

`01-scenarios.json` in the run folder, written by `scenario-analyst`, checked by `dtt validate-scenarios` (its rules mirror `schemas/scenarios.schema.json`; if they differ, the validator decides).

```json
{
  "target": "src/Orders/DiscountCalculator.cs",
  "request": { "userText": "test Apply with 3 tests, only the boundary at 100", "count": { "max": 3 }, "focus": "the boundary at 100" },
  "scope": { "requested": "3 tests", "produced": 3, "leftOut": ["4 throws"] },
  "scenarios": [
    {
      "id": "S001",
      "member": "DiscountCalculator.Apply",
      "construct": { "type": "branch", "file": "src/Orders/DiscountCalculator.cs", "line": 42 },
      "input": "total = 100.00, customer is premium",
      "expected": "returns 90.00",
      "expectedBasis": "xml-doc",
      "rationale": "XML doc says premium customers get 10% off.",
      "kind": "boundary"
    }
  ]
}
```

- `construct.type`: `branch, constant, throw, null-conditional, switch-arm, loop, dependency-call, await, signature, other`.
- `kind`: `normal, boundary, exception, dependency, async, state, ambiguous`.
- `expectedBasis`: `name, xml-doc, usage, contract, user-stated, unknown` (`unknown` only with `kind: ambiguous`). `user-stated` means the user's own words.
- Optional per scenario: `origin` (`user|derived`), `group` (`G1`..., scenarios that become one parameterized test), `conflict` (one line: the user's value disagrees with the code or docs; kept, never repaired). (`existingTest` is accepted in a file but never written: existing tests are not read.)
- `request` (optional) records the user's wishes: `userText` verbatim, `targets[{file, members}]`, `count{max, per: total|target}`, `cases[{member, input, expected}]` (at most 20), `focus` (free text), `exclude`, `form` (`separate|grouped`), `names`. Only what the user really said. Cases only, no amount, no focus: `count.max` = number of cases. The validator enforces the caps and the user-stated rule.
- Text fields are at most 300 characters, one line; at most 200 scenarios.
