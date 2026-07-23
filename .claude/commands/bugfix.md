---
description: Startet einen disziplinierten Bugfix-Zyklus (Verstehen → Rot → Ursache → Fix → Verifikation)
argument-hint: <Fehlerbeschreibung oder FINDINGS-Nummer>
---

Behebe folgenden Fehler: **$ARGUMENTS**

Lies zuerst `.claude/skills/bugfix/SKILL.md` und `.claude/skills/testing/SKILL.md` und
halte dich ohne Ausnahme daran. Arbeite die Phasen einzeln ab und **stoppe an den
beiden Freigabe-Toren**.

**Phase 0 — VERSTEHEN.**
Leite das korrekte Verhalten aus der Fachlichkeit, der Spec, den Docstrings oder den
Feldnamen her — **nicht** aus dem verdächtigen Code. Untersuche die betroffene Stelle
und formuliere: erwartetes Verhalten (ein Satz), beobachtetes Verhalten (ein Satz),
exakte Auslösebedingungen. Frag nach, wenn die Fachlichkeit mehrdeutig ist.

**Phase 1 — ROT.**
Betrifft der Fehler das Frontend, klassifiziere ihn zuerst nach der
Frontend-Sektion der Skill (A: serverseitig gerendert → pytest gegen das Markup;
B: reine Client-Logik → `static/lib/*.mjs` extrahieren und mit
`node --test "tests/js/**/*.test.mjs"` testen; C: Interaktion/Darstellung →
manuelle Reproduktion, ausdrücklich als solche kennzeichnen).

Schreib den Regressionstest auf der niedrigstmöglichen Ebene, die den Fehler
reproduziert, und assertiere das **korrekte** Verhalten. Existiert bereits ein
`xfail(strict=True)`-Test aus `docs/FINDINGS.md` zu diesem Defekt, ist **das** der
Regressionstest — keinen zweiten schreiben. Führ ihn aus und zeig mir, dass er aus
dem richtigen Grund fehlschlägt.
→ **STOPP. Zeig mir Analyse, Test und Rot-Ausgabe, warte auf Freigabe.**

**Phase 2 — URSACHE.**
Benenne die tatsächliche Fehlerursache und grenze sie klar vom Symptom ab. Prüfe per
Suche, ob derselbe Fehlertyp an weiteren Stellen vorkommt. Schlag den minimalen Fix
vor — kein Abfangen breiterer Exceptions, keine Sonderbehandlung der Beispieleingabe,
kein Refactoring.
→ **STOPP. Zeig mir Ursache, Geschwister-Fundstellen und Fix-Vorschlag, warte auf Freigabe.**

**Phase 3 — GRÜN.**
Minimalen Fix umsetzen. Nichts Unverwandtes mit ändern. Klärt der Fix einen
`xfail`-Eintrag, entferne den Marker.

**Phase 4 — VERIFIKATION.**
`pytest -q` und `pytest --cov=<berührtes_modul> --cov-report=term-missing -q`.

Abschlussbericht in genau dieser Form:
- erwartetes vs. beobachtetes Verhalten
- Ursache (Datei:Zeile) — ausdrücklich abgegrenzt vom Symptom
- Regressionstest (Name) + Beleg, dass er vorher rot war
- geänderte Bestandstests: welcher, und unter welchem der **zwei** erlaubten Gründe
  (Test hatte den Bug festgeschrieben / Test war an Implementierungsdetails gekoppelt)
  — sonst keine Teständerung
- Geschwister-Fundstellen desselben Fehlertyps (als neue Zeilen in `docs/FINDINGS.md`)
- `docs/FINDINGS.md`: betroffene Zeile auf `fixed` gesetzt
