# VMS Test Plan — retrofitting coverage

## Setup (once)

```bash
pip install -r requirements.txt -r requirements-dev.txt   # Docker daemon must be running
pytest tests/test_smoke.py -q                             # verify the harness (spins up PostgreSQL)
```

`pytest` alone runs the full suite and writes `coverage.xml` (machine-readable) and
`htmlcov/index.html` (review). Fast loop during development:

```bash
pytest -m "unit or route" -q          # skips the heavier integration paths
pytest --cov-report=term-missing -q   # see uncovered lines/branches inline
```

## Priority order (by risk, not by file size)

Coverage is retrofitted **risk-first**. Work top-down; don't chase a coverage % on
low-risk modules before the money paths are locked.

| # | Target | Why first | Key branches to pin |
|---|--------|-----------|---------------------|
| 1 | `kms.py`, `security.py` | Secret confidentiality; a silent bug leaks or corrupts stored credentials | encrypt/decrypt round-trip, empty input → None, KMS-available vs SECRET_KEY fallback |
| 2 | `invoice_routes.py` + `SequentialNumber` logic | Financial correctness; duplicate/again-off-by-one invoice numbers are the worst-case bug | per-type counter independence, no collision on repeated allocation, `laufende_nummer`/`nummer_typ` assignment |
| 3 | `auth.py` | Authn/authz boundary | verify_password true/false, invitation create → expiry (freeze_time) → complete, inactive user cannot log in |
| 4 | `verleih_routes.py` | Core domain (Verleih/return state machine) | return rejects already-returned, status transitions, responsible-user handling |
| 5 | `kanboard_client.py` | External parsing surface | `parse_description` on real + malformed strings, `_format_date_with_time`, column lookup (via `mock_kanboard`) |
| 6 | `inventory_routes.py`, `settings_routes.py`, `template_*` | CRUD + encrypted settings | create/update/delete, encrypted field round-trip, not-found ids |
| 7 | `odt_processor.py` (except the LibreOffice subprocess) | Template filling | placeholder substitution correctness; `convert_to_pdf` stubbed via `no_libreoffice` |
| 8 | `app.py` view functions, `email_client.py` | Glue; lower blast radius | dashboard/API routes, mail composition via `mailbox` |

Set a **branch-coverage gate** once module 1–4 are green, e.g. add
`--cov-fail-under=70` to `pyproject.toml`, then ratchet it up as the suite grows.
Excluded from the denominator (see `pyproject.toml`): `migrate_*`, `kms_setup.py`.

## Driving Claude Code (the coverage loop)

The `.claude/skills/testing/SKILL.md` skill loads automatically. Work **one module per
session**, iterating against real coverage data rather than generating everything blind.

Per module, use this prompt shape:

> Read `.claude/skills/testing/SKILL.md`, then `invoice_routes.py` and the
> `SequentialNumber` model in `models.py`. Run `pytest --cov=invoice_routes
> --cov-report=term-missing -q`. From the missing lines/branches, write tests for the
> **highest-risk uncovered branches first** (sequential-number allocation and the
> invoice-creation route). Use only the conftest fixtures. After writing, re-run the
> same command and report the new branch coverage and which branches remain uncovered
> and why.

Then iterate:

> Cover the remaining uncovered branches in `invoice_routes.py`. For any branch that is
> genuinely unreachable, mark it `# pragma: no cover` with a one-line justification
> instead of contriving a test.

Use **plan mode** for modules 2 and 4 (stateful logic): have Claude Code produce a
per-branch test plan first, you approve it, then it implements — this prevents a wall
of brittle, tautological tests.

## Harness-Besonderheit: KMS-Isolation

`kms.py` hat zwei Eigenschaften, die Zweige unerreichbar machen, wenn man sie nicht
neutralisiert. Die autouse-Fixture `_kms_isolated` in `tests/conftest.py` erledigt
das — nicht entfernen:

- `_master_key` / `_secrets` sind prozessweite Caches. Sobald ein Test einen Key
  lädt, kurzschließen alle späteren Loads auf dem Cache, und die Branches „Datei
  fehlt" / „Key zu kurz" sind nicht mehr erreichbar → Testreihenfolge-Abhängigkeit.
  Seit `save_secrets` den Cache mitschreibt, gilt das auch nach jedem Schreibvorgang:
  Tests, die den Entschlüsselungspfad prüfen wollen, brauchen ein explizites
  `kms.clear_cache()` zwischen Schreiben und Lesen.
- `.env` setzt `KMS_MASTER_KEY_PATH` auf einen *relativen* Pfad, der im Repo
  existiert. `is_kms_available()` liefert dadurch in Tests fälschlich `True`,
  `security._get_fernet` nimmt immer den KMS-Zweig, und der komplette
  `SECRET_KEY`-Fallback wäre toter Code.

Zweite Falle, unabhängig von KMS: `_app` ist **session-scoped**. Wer `app.config`
in einem Test verändert (z. B. `SECRET_KEY`), muss `monkeypatch.setitem` benutzen —
eine rohe Zuweisung leckt in alle folgenden Tests.

## Definition of done (per module)

- Branch coverage ≥ target, **and** every unhappy path (auth failure, invalid input,
  not-found, already-in-terminal-state) has an explicit test.
- No test asserts only "was called"; each asserts an observable outcome.
- No network / mail / LibreOffice contact (enforced by fixtures).
