---
name: vms-bugfix
description: >
  Disciplined workflow for fixing a defect in VMS — establish the correct behaviour
  from the domain, reproduce it with a failing regression test, find the root cause,
  fix minimally, verify. Use whenever something is reported as broken, wrong,
  crashing, or behaving unexpectedly, and when clearing an entry from
  docs/FINDINGS.md. For new functionality use the `vms-tdd` skill; for retrofitting
  tests onto untested code use `vms-testing`.
---

# Fixing a bug in VMS

A fix is not done when the symptom disappears. It is done when a test that proves
the defect existed now passes, the cause is understood, and the same mistake cannot
recur silently.

## The loop — do not skip or reorder

### 0. UNDERSTAND — what *should* happen
Derive the correct behaviour from the domain, the spec, the docstring or the field
names — **never from the code under suspicion**. Reading the buggy implementation to
decide what is correct is circular and is how a defect gets promoted to a
requirement. If the correct behaviour is genuinely ambiguous, ask; do not choose.

State, before touching anything:
- expected behaviour, in one sentence
- observed behaviour, in one sentence
- exact trigger conditions (input, state, sequence, timing, permissions)

### 1. RED — reproduce with a failing test
Write a test that fails **because of this defect** and would have caught it.

- Put it at the **lowest level that still reproduces**: pure unit test if the rule is
  pure, otherwise persistence, otherwise the endpoint. A route-level test for a
  pure-logic bug is a weak regression test.
- Assert the **correct** expectation, not the current output.
- Run it. Confirm it fails for the intended reason (assertion / the real exception),
  not an unrelated error.
- **If an `xfail(reason="BUG: …", strict=True)` test from `docs/FINDINGS.md` already
  covers this defect, it IS the regression test** — do not write a second one.
  Confirm it currently xfails; the marker comes off in step 3.

Never proceed to a fix without a red test. If a defect is truly not reproducible in a
test (environment/infrastructure only), say so explicitly and record why.

### 2. ROOT CAUSE — the defect, not the symptom
Locate the actual faulty logic and name it. Distinguish clearly:
- the **cause** (the wrong condition, the missing check, the misordered assignment)
- the **symptom** (the exception, the wrong number, the 500)

Reject these as "fixes": widening a caught exception, adding a `None` guard at the
call site while the producer still returns `None` wrongly, special-casing the input
from the bug report, retrying, or reordering unrelated code until it passes.

Also ask: **is this an instance of a class?** Grep for the same pattern elsewhere
(same mis-ordered validation, same swallowed exception, same tz-naive comparison).
Record siblings found in `docs/FINDINGS.md`; fix them separately, not in this change.

### 3. GREEN — minimal fix
Change the smallest amount of code that removes the cause. No refactoring, no
cleanup, no unrelated improvements in the same change — those come after, with the
now-green test as the net.

If the fix clears an `xfail` entry, remove the marker (a strict xfail that starts
passing fails the suite — that is the intended signal) and set the row in
`docs/FINDINGS.md` to `fixed`.

### 4. VERIFY
```bash
pytest -q                                        # whole suite green, no collateral
pytest --cov=<touched_module> --cov-report=term-missing -q
node --test "tests/js/**/*.test.mjs"             # only if client-side code was touched
```
The regression test passes, and no other test changed status.

## Frontend bugs

The loop above applies unchanged — only the mechanism for step 1 (RED) differs.
Classify the defect first; picking the wrong class wastes the most time here.

**Class A — rendered server-side.** Wrong value shown, element missing for a given
state, wrong link target, broken escaping, a wrong `{% if %}` condition. These are
backend defects wearing a frontend costume and need **no new tooling**: assert
against the rendered markup with the existing pytest fixtures.

```python
@pytest.mark.route
def test_verleih_zeigt_rueckgabedatum(auth_client, ...):
    resp = auth_client.get("/verleih")
    assert "31.12.2026".encode() in resp.data
```
Assert on a distinctive substring, not on whole markup blocks — otherwise the test
breaks on every unrelated styling change.

**Class B — pure client-side logic.** Formatting, parsing, filtering, validation.
Testable with Node's built-in runner, zero dependencies, no build step:

```bash
node --test "tests/js/**/*.test.mjs"
```
Quote the glob; do not pass the directory (`node --test tests/js` resolves the path
as a module and fails).

Only ES modules under `static/lib/` are testable — they must stay free of DOM, fetch
and globals. If the buggy logic is entangled with the DOM inside a script, **extract
it to `static/lib/*.mjs` first**, verify the behaviour is unchanged, then write the
red test against the extracted function. That extraction is part of the fix, not an
optional cleanup: logic that can only be reached through the DOM cannot have a
regression test.

Wiring the module into a classic script — module scripts are deferred and run *after*
classic ones, so only reference the bridge from event handlers or after
`DOMContentLoaded`, never at the classic script's top level:

```html
<script type="module">
  import * as fmt from '/static/lib/format.mjs';
  window.VMSFormat = fmt;
</script>
```

Watch for the same rule implemented on both sides (a Python helper and its JavaScript
twin). Fixing only one half leaves the defect live on the other; fix both in the same
change and record the duplication in `docs/FINDINGS.md`.

**Class C — interaction and presentation.** Event handling, modal behaviour, layout,
mobile rendering. Out of scope for automated regression here — browser automation is
not worth its cost for this project. Reproduce and document manually: record the exact
steps, browser and viewport in the report, state explicitly that step 1 was manual,
and re-check those steps after the fix. Every other step of the loop still applies.

## Changing existing tests — the hard rule

A failing pre-existing test is evidence, not an obstacle. **Do not edit a test to make
a fix pass.** Exactly two situations justify touching one, and both must be stated
explicitly in the report:

1. **The test encoded the bug.** Common with tests retrofitted onto untested code:
   the old test asserted the wrong behaviour as correct. Correct the assertion and say
   which behaviour it wrongly pinned.
2. **The test was coupled to implementation detail**, not behaviour (asserted internal
   calls, private helpers, ordering). Rewrite it against the observable outcome.

Anything else — loosening an assertion, deleting a case, adding a skip, broadening a
range until it passes — is forbidden. If the fix and a test disagree and neither of
the two cases applies, the fix is wrong.

## Test conventions
All rules of `.claude/skills/testing/SKILL.md` apply unchanged: the fixtures in
`tests/conftest.py`, the markers, the PostgreSQL testcontainer, no network/mail/
LibreOffice contact, no "was called" as a sole assertion, no tautological expectations.

Determinism matters more here than anywhere: pin time with `freeze_time` rather than
relying on the clock, and never let a regression test depend on execution order or on
rows created by another test.

## Definition of done
Correct behaviour stated independently of the buggy code · a regression test that was
red for the right reason is now green · root cause named, not just the symptom ·
minimal change · full suite green · any edited pre-existing test justified under one
of the two allowed cases · `docs/FINDINGS.md` updated (this entry `fixed`, siblings
logged as new rows).
