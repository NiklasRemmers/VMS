---
name: vms-testing
description: >
  Conventions for writing pytest tests for this Flask/SQLAlchemy codebase (VMS).
  Use whenever generating, extending, or fixing tests, improving coverage, or
  adding a test for a route, model, or module. Enforces the fixture-based harness
  in tests/conftest.py and the PostgreSQL-testcontainer strategy.
---

# Writing tests for VMS

VMS is a Flask app with a **module-level `app`** (no factory), **SQLAlchemy 2.x on
PostgreSQL** (models use `JSONB` and tz-aware `DateTime` — never assume SQLite),
flask-login auth, flask-wtf CSRF, flask-limiter, KMS/Fernet crypto, ODT→PDF via a
LibreOffice subprocess, and a Kanboard HTTP client. The harness already solves the
hard wiring; do not re-invent it.

## Hard rules

1. **Never spin up your own app or engine.** Use the fixtures in `tests/conftest.py`:
   `app`, `client`, `auth_client`, `app_ctx`, `db_session`, `user`. The `app`
   fixture truncates all tables after each test, so tests are order-independent —
   never rely on rows created by another test.
2. **Never let a test touch the network, mail server, or LibreOffice.**
   - Kanboard: use the `mock_kanboard` fixture (patches `kanboard_client._make_request`).
   - ODT→PDF: use the `no_libreoffice` fixture (stubs `odt_processor.convert_to_pdf`).
   - Mail: assert with the `mailbox` fixture; sending is suppressed.
3. **Authenticated routes** → use `auth_client`, not a manual login POST. CSRF and
   rate limiting are disabled in test config; don't add tokens or `@limiter` handling.
4. **Time-dependent logic** (invitation expiry, sequential invoice/umbuchung numbers,
   the daily reconcile job) → pin time with `freezegun.freeze_time`, never `sleep`.
5. **Crypto**: `security.encrypt_value/decrypt_value` require `app_ctx` (they read
   `current_app`). `kms.*_secret` are pure — pass an explicit `master_key`, no context.
6. One behaviour per test. Arrange–Act–Assert, blank line between phases. German
   domain nouns from the code (Verleih, Umbuchung, Rechnung, Leihanfrage) are kept
   verbatim in names and data.

## Structure & markers

- File per module under test: `tests/test_<module>.py` (e.g. `test_invoice_routes.py`).
- Mark every test: `@pytest.mark.unit` (no DB/context), `@pytest.mark.integration`
  (DB), or `@pytest.mark.route` (endpoint via client). `slow` for anything heavy.
- Test names state the behaviour and the branch:
  `test_next_invoice_number_increments_per_type`,
  `test_return_rental_rejects_already_returned`.

## What a good test asserts

- **Routes**: status code **and** the persisted side effect (row created/updated) or
  the JSON/flash payload — not just `200`. Cover the unhappy branches: unauthenticated,
  missing/invalid form field, not-found id, CSRF-exempt `/api/` vs form routes.
- **Sequential numbers / invoicing**: correctness under concurrency-shaped sequences
  (two allocations don't collide; per-type counters are independent). This is the
  highest-risk logic in the system — assert exact values, not just "changed".
- **Encryption fields** (settings: SMTP/IMAP/Kanboard secrets): stored value is
  ciphertext, round-trips back to plaintext, and empty input yields `None`.
- **Kanboard parsing** (`parse_description`, `_format_date_with_time`): feed real
  description strings; assert the parsed dict and date normalisation, incl. malformed
  input returning empty/None rather than raising.

## Anti-patterns (reject these)

- Asserting a function "was called" as the *only* assertion — assert the observable
  outcome instead.
- Mocking `database`/`get_session` — use the real testcontainer DB; only mock the
  three external boundaries above.
- Copying a value the code produces into the expected value (tautological test).
- Broad `try/except` or `assert resp.status_code != 500` — assert the exact contract.

## Reporting suspected bugs (do not silently pin wrong behaviour)

Retrofit tests must not freeze buggy behaviour as the expected value. When the
domain-correct result and the code's actual result diverge, the code is the suspect.

Treat these as anomalies worth flagging:
- Behaviour contradicts the docstring, a German inline comment, or the field name.
- Off-by-one or collision in sequential invoice/umbuchung numbers.
- A branch that can never be reached, or a condition that is always true/false.
- A bare `except:`/broad `except Exception` that swallows an error and returns
  `None`/`[]` where the caller assumes a real value.
- A route with a side effect but no `@login_required` / ownership check.
- tz-naive vs tz-aware datetime mismatch, or a string date compared to a real date.
- Status transition that is accepted when it should be rejected (e.g. returning an
  already-returned Verleih).

When you find one:
1. Write the test asserting the **domain-correct** expectation (what *should* happen),
   not what the code currently does.
2. Mark it `@pytest.mark.xfail(reason="BUG: <one line>", strict=True)` so it documents
   the defect without breaking the suite — it flips to a failure (xpass) the moment the
   bug is fixed, which is your signal to drop the marker.
3. Record it in `docs/FINDINGS.md` (see the table there) and in your return summary.

Never wrap a suspected bug in an assertion that makes it look correct.

## Reference examples

`tests/test_smoke.py` contains a canonical unit, route, authed-route, and integration
test. Mirror those patterns.
