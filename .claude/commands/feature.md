---
description: Startet einen TDD-Zyklus für ein neues VMS-Feature (Spec → Red → Green → Refactor → Verify)
argument-hint: <Feature-Beschreibung>
---

Baue folgendes Feature strikt test-getrieben: **$ARGUMENTS**

Lies zuerst `.claude/skills/tdd/SKILL.md` und `.claude/skills/testing/SKILL.md` und
halte dich ohne Ausnahme daran. Arbeite die Phasen einzeln ab und **stoppe an den
beiden Freigabe-Toren**.

**Phase 0 — SPEC.**
Untersuche die betroffenen bestehenden Module, bevor du schreibst. Fülle
`docs/SPEC_TEMPLATE.md` aus und lege sie als `docs/specs/<feature>.md` an. Stelle mir
alle offenen fachlichen Fragen (Berechtigung, Idempotenz, verbotene Statusübergänge,
Nummernvergabe, Zeitzone) — erfinde keine Regel.
→ **STOPP. Zeig mir die Spec und warte auf Freigabe.**

**Phase 1 — RED.**
Übersetze jedes Akzeptanzkriterium und jeden Fehlerfall in Tests. Nutze
ausschließlich die Fixtures aus `tests/conftest.py`. Führe sie aus und zeig mir, dass
sie **aus dem richtigen Grund** fehlschlagen (Assertion, nicht Import-/NameError).
→ **STOPP. Zeig mir die Tests und die Rot-Ausgabe, warte auf Freigabe.**

**Phase 2 — GREEN.**
Minimale Implementierung. Orientiere dich an der Struktur, die vergleichbare Features
im Repo aktuell verwenden, und halte die Trennung ein: Geschäftsregeln in reine,
ohne App-Kontext und ohne Datenbank aufrufbare Funktionen; die Delivery-Schicht
(Route/Job/CLI) übersetzt nur. Bestehende, bereits überdimensionierte Module nur um
die minimale Verdrahtung ergänzen. Keine spekulativen Parameter oder Zweige.
Iteriere bis alle Tests grün sind.

**Phase 3 — REFACTOR.**
Struktur verbessern, Tests dabei **nicht** anfassen. Muss ein Test geändert werden,
war er an Implementierungsdetails gekoppelt — melde das statt es stillschweigend zu
reparieren.

**Phase 4 — VERIFY.**
`pytest -q` und `pytest --cov=<neues_modul> --cov-report=term-missing -q`.

Abschlussbericht in genau dieser Form:
- Akzeptanzkriterium → zugehöriger Testname (vollständige Tabelle, keine Lücke)
- Branch-Coverage des neuen Moduls
- verbleibende unabgedeckte Zweige + Begründung
- FINDINGS: Auffälligkeiten in berührtem Bestandscode (nach
  `docs/FINDINGS.md`-Protokoll melden, nicht eigenmächtig fixen)
