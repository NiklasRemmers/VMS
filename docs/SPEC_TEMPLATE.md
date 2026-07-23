# Spec: <Feature-Name>

Status: draft | approved | implemented · Datum: <YYYY-MM-DD>

Wird vor jeder Zeile Code ausgefüllt und freigegeben (`.claude/skills/tdd/SKILL.md`,
Schritt 0). Kopie ablegen unter `docs/specs/<feature>.md`.

## 1. Zweck

Ein Absatz: welches konkrete Problem im Betrieb löst das Feature? Wer nutzt es?

## 2. Domänenregeln

Die fachlichen Regeln in Prosa — die Wahrheitsquelle, gegen die getestet wird.
Nicht „wie der Code es macht", sondern „was gelten muss".

- ...

## 3. Akzeptanzkriterien

Nummeriert, jedes einzeln als Test formulierbar. Diese Liste ist die
Test-Checkliste — jede Nummer bekommt einen benannten Test.

| # | Kriterium | Testname |
|---|-----------|----------|
| 1 | | |
| 2 | | |

## 4. Fehlerfälle & Randbedingungen

Explizit, nicht implizit. Mindestens durchdenken:

| Fall | Erwartetes Verhalten |
|------|----------------------|
| Nicht authentifiziert | |
| Fehlende/ungültige Eingabe | |
| ID nicht gefunden | |
| Zweiter/doppelter Aufruf (Idempotenz) | |
| Unzulässiger Statusübergang | |
| Nebenläufigkeit / Nummernvergabe | |
| Externer Dienst nicht erreichbar (Kanboard, Mail, LibreOffice) | |

## 5. Datenmodell

Neue/geänderte Tabellen, Spalten, Typen, Constraints. Migration nötig? Rückwärts-
kompatibel? Zeitzonen-behaftete `DateTime`? Deutsche Datumsformate an der Grenze?

## 6. Schnittstelle

Einstiegspunkt(e), Eingabefelder, Antwortform, Status-/Fehlercodes.
Berechtigung: reicht die Standard-Authentifizierung, oder ist zusätzlich eine
Ownership-/Rollenprüfung nötig?

## 7. Architektur

An der Struktur ausrichten, die vergleichbare Features im Repo aktuell verwenden.

- Wo liegen die Geschäftsregeln (rein, ohne App-Kontext/DB aufrufbar)?
- Wo liegt die Delivery-Schicht (Route/Job/CLI), die nur übersetzt?
- Berührte bestehende Module (und was dort minimal verdrahtet wird):
- Externe Grenzen (im Test gemockt):

## 8. Nicht im Umfang

Was bewusst weggelassen wird — verhindert Scope Creep und spekulative Zweige.

## 9. Offene Fragen

Vor Freigabe zu klären. Keine Annahme erfinden.
