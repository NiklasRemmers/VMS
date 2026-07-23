---
name: vms-tdd
description: >
  Test-driven workflow for building NEW features or changing behaviour in VMS.
  Use whenever a feature, endpoint, bugfix, or behaviour change is requested —
  spec first, failing test second, implementation third, verification last.
  Enforces clean-code structure (thin routes, pure domain logic) for this
  Flask/SQLAlchemy codebase. For retrofitting tests onto untested existing code,
  use the `vms-testing` skill instead.
---

# TDD for VMS

Existing code is retrofitted with tests (`vms-testing`). **New** code is written
test-first. Never write production code without a failing test that demands it.

## The loop — do not skip or reorder

### 0. SPEC (understand before writing anything)
Produce `docs/specs/<feature>.md` from `docs/SPEC_TEMPLATE.md` and get it approved
before any code. It must state: the domain rules in prose, the acceptance criteria
as a numbered list, every edge case and error path, and what is explicitly out of
scope. Ask about anything ambiguous — do not invent a rule.

Domain questions that are almost always underspecified here — ask them:
- Who may do this? (`@login_required` alone, or an ownership/role check?)
- What happens on the second, duplicate, or out-of-order call?
- Which state transitions are forbidden? (e.g. an already-returned Verleih)
- Are numbers sequential/gapless per type? What on rollback?
- Timezone: naive or aware? German date format on the boundary?

### 1. RED — write the failing test
Translate every acceptance criterion into a test **before** the implementation.
Run it and confirm it fails **for the intended reason** (assertion, not
ImportError/NameError from a missing symbol you never designed). A test that has
never been red proves nothing.

Order: pure domain logic first, then persistence, then the route. Cover happy
path *and* every error path from the spec in this step — not later.

### 2. GREEN — minimal implementation
Write the simplest code that makes the tests pass. No speculative parameters, no
"we might need this" branches — unbuilt flexibility is untested flexibility.

### 3. REFACTOR — with the tests as a net
Only now improve structure (see clean-code rules below). Tests must stay green and
must NOT be edited during refactoring; if a refactor forces a test change, the test
was coupled to implementation detail, not behaviour — fix the test's design.

### 4. VERIFY
```bash
pytest -q                                          # full suite green
pytest --cov=<new_module> --cov-report=term-missing -q
```
Every acceptance criterion maps to a named test. Uncovered branch = missing case in
the spec; go back to step 0, do not paper over it.

## Clean-code rules

Principles, not a fixed file layout. Follow the conventions the codebase actually
uses at the time you work in it: before creating anything, look at how comparable
features are currently structured and place new code consistently. If the structure
below and the existing structure disagree, follow the existing one and say so.

**Separate policy from plumbing.** Business rules belong in importable,
side-effect-free functions that take plain arguments and return plain values —
no framework objects, no global request/session state, no I/O. Delivery code
(HTTP handler, CLI entry, scheduled job) only translates: parse and validate input →
call one domain function → map the result to a response. If a rule can only be
exercised by issuing an HTTP request, it is in the wrong place.

The practical test: every business rule must be reachable by a `@pytest.mark.unit`
test with no app context and no database. When that is impossible, extract until it is.

**One reason to change per unit.** A module, class or function serves a single
concern. Adding a feature should mean adding a unit, not widening an existing one.

**Do not grow a module that is already oversized.** Prefer a new, cohesive unit
alongside it; touch the oversized one only for the minimal wiring (registration,
export, import). Rough ceilings: function ~30 lines and 3 nesting levels, module
~400 lines — exceeding one is a signal to split, not a hard error.

**Explicit dependencies.** Pass what a function needs as arguments; do not reach for
module-level globals, ambient context or cached singletons inside domain code.
Anything cached process-wide must be resettable, or tests become order-dependent.

**Resources are owned at the boundary.** Open a transaction/session/file/connection
in the delivery layer, one per unit of work, and hand already-loaded data down.
Domain functions never open or commit their own.

**Errors are specific and loud.** Raise a meaningful, typed exception where the rule
is violated; translate it to a user-facing response only at the boundary. Never
swallow an exception, and never return an empty/`None` value to signal failure where
the caller expects a real one — that class of silent bug is what
`docs/FINDINGS.md` exists to record.

**Single source of truth.** A rule, constant or format lives in exactly one place.
Duplication that must be kept in sync is a defect, not a style issue.

**Secrets never travel in plaintext.** Credential-like values go through the
project's encryption/KMS layer and are never logged, echoed or committed.

**Names carry the domain.** Keep the project's established domain vocabulary verbatim
in its original language (here: German nouns such as Verleih, Umbuchung, Rechnung,
Leihanfrage); write everything else in English. Functions are verbs; booleans read as
predicates (`is_`, `has_`, `can_`).

**Build only what a test demands.** No speculative parameters, options or branches —
unbuilt flexibility is untested flexibility, and it decays into dead code.

## Test conventions

All rules of `.claude/skills/testing/SKILL.md` apply unchanged — same fixtures
(`app`, `client`, `auth_client`, `db_session`, `user`, `mock_kanboard`,
`no_libreoffice`, `mailbox`), same markers, same PostgreSQL testcontainer, same ban on
network/mail/LibreOffice contact and on "was called" as a sole assertion.

Additionally, test **behaviour, not implementation**: assert observable outcomes
(response, persisted row, raised exception), never internal call order or private
helpers. That is what makes step 3 possible.

## Definition of done
Spec approved · every criterion has a named test · suite green · new code's branch
coverage ≥ 90 % · business rules unit-testable without app context or database ·
no oversized module grown beyond minimal wiring · suspected bugs in touched code
logged in `docs/FINDINGS.md`.
