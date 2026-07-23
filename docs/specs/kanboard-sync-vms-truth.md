# Spec: Kanboard-Sync — VMS als Wahrheitsquelle

Status: implemented · Datum: 2026-07-23

Wird vor jeder Zeile Code ausgefüllt und freigegeben (`.claude/skills/tdd/SKILL.md`,
Schritt 0).

## 1. Zweck

Beim Klick auf „Synchronisierung" in den Leihanfragen gleicht VMS die Kandidaten mit
dem Kanboard ab. **Heute gewinnt Kanboard:** für einen bereits verknüpften Kandidaten
überschreibt der Sync alle VMS-Felder mit den aus der Kanboard-Beschreibung geparsten
Werten (`sync_with_kanboard`, `vms/clients/email_client.py:570–609`). Trägt eine
Sachbearbeiterin in VMS Daten nach (E-Mail, Name, Telefon, Anschrift …), werden diese
beim nächsten Sync wieder gelöscht, weil Kanboard sie im Freitext nicht hat.

Dieses Feature invertiert die Richtung: **VMS ist die Wahrheit.** Für verknüpfte
Kandidaten wird nicht mehr aus Kanboard gezogen, sondern der Kanboard-Task aus den
VMS-Feldern aktualisiert. Neue, noch unverknüpfte Kanboard-Tasks werden wie bisher als
neue Kandidaten importiert (der Eingangskanal für frische Anfragen).

## 2. Domänenregeln

- **Verknüpfter Kandidat = VMS ist Wahrheit.** Ein Kandidat mit gesetzter
  `kanboard_task_id`, für den ein gleichnamiger Task existiert, wird beim Sync
  **nie** aus Kanboard überschrieben. Stattdessen wird der Kanboard-Task an den
  VMS-Stand angeglichen.
- **Der Push umfasst:** Titel (`veranstaltungsname`, ersatzweise `subject`),
  Fälligkeitsdatum (`datum`), Tags (`tags`) und die **Beschreibung**, die aus den
  strukturierten VMS-Feldern neu erzeugt wird.
- **Beschreibungsformat.** Die erzeugte Beschreibung nutzt exakt die Labels, die
  `kanboard_client.parse_description` zurücklesen kann, sodass ein erneuter Pull
  (falls jemals) dieselben Felder ergäbe. Leere/`None`-Felder werden weggelassen,
  nicht als leere Zeile geschrieben.
- **Datum bleibt maßgeblich über `date_due`.** Das Fälligkeitsdatum wird als
  Kanboard-`date_due` gepusht (über das bestehende `_format_date_with_time`), nicht
  über eine „Datum:"-Zeile im Freitext. Ist `datum` in VMS leer, wird ein in Kanboard
  vorhandenes Datum **nicht** gelöscht (kein Clearing).
- **Idempotenz.** Gepusht wird nur, wenn der VMS-Sollzustand vom aktuellen
  Kanboard-Task abweicht (Titel ODER Beschreibung ODER Tags ODER Datum). Ist alles
  gleich, unterbleibt der API-Call und der Task zählt **nicht** als „updated".
- **Unverknüpfter Task = Pull.** Ein Kanboard-Task, dessen `id` keinem VMS-Kandidaten
  entspricht, wird wie bisher als neuer Kandidat (`status=PROCESSED`) angelegt. VMS
  kann für einen ihm unbekannten Task keine Wahrheit sein.
- **Best-effort.** Kanboard-Fehler brechen den Request nie ab: Ein globaler Fehler
  beim Task-Abruf liefert `{'updated':0,'created':0}`; ein Fehler bei genau einem
  Task (Push oder Anlegen) wird geloggt und geschluckt, die übrigen Tasks laufen
  weiter. Ein fehlgeschlagener Push zählt nicht als „updated".
- **Zählung.** Rückgabe bleibt `{'updated': <erfolgreiche Pushes>, 'created':
  <neu angelegte Kandidaten>}` — gleiche Keys, damit Route und UI
  (`kanboard_updated`) unverändert funktionieren.

## 3. Akzeptanzkriterien

| # | Kriterium | Testname |
|---|-----------|----------|
| 1 | `build_task_description` erzeugt aus VMS-Feldern die kanonisch gelabelte Beschreibung; `parse_description` liest sie verlustfrei zurück (Round-Trip) | `test_build_task_description_roundtrips_through_parse_description` |
| 2 | `build_task_description` lässt leere/`None`-Felder weg (keine leeren Label-Zeilen) | `test_build_task_description_omits_empty_fields` |
| 3 | `kanboard_date_due_to_iso` wandelt einen Unix-Timestamp (Europe/Berlin) in `YYYY-MM-DD`; `'0'`/leer → `''` | `test_kanboard_date_due_to_iso_converts_timestamp` / `_empty_returns_blank` |
| 4 | Verknüpfter Kandidat, dessen VMS-Daten von Kanboard abweichen → `update_task` wird mit Titel, neu erzeugter Beschreibung, Tags und Datum aufgerufen; `updated=1` | `test_sync_pushes_vms_data_to_kanboard_when_diverged` |
| 5 | Regression: In VMS nachgetragene Felder (`email_address`, `vorname_nachname`, `telefon`, `anschrift`) werden vom Sync **nicht** überschrieben/gelöscht | `test_sync_does_not_overwrite_vms_fields_from_kanboard` |
| 6 | Verknüpfter Kandidat, dessen VMS-Stand mit Kanboard identisch ist → kein `update_task`-Aufruf, `updated=0` (Idempotenz) | `test_sync_in_sync_candidate_does_not_push_or_count` |
| 7 | Unverknüpfter Kanboard-Task → neuer Kandidat angelegt, `created=1` (Pull erhalten) | `test_sync_creates_new_candidate_for_unlinked_task` |
| 8 | `get_leihanfragen_tasks` wirft → `{'updated':0,'created':0}` | `test_sync_swallows_get_tasks_error_and_returns_zero_counts` |
| 9 | Push eines Tasks wirft → geschluckt, `updated` zählt ihn nicht, andere Tasks laufen weiter | `test_sync_swallows_per_item_push_error_but_keeps_other_items` |
| 10 | `datum` in VMS leer → kein Datum-Clearing in Kanboard (Push ohne `date_due`, bzw. Task gilt nur bzgl. Datum nicht als abweichend) | `test_sync_empty_vms_datum_does_not_clear_kanboard_date` |

## 4. Fehlerfälle & Randbedingungen

| Fall | Erwartetes Verhalten |
|------|----------------------|
| Nicht authentifiziert | Route `/api/emails/sync` unverändert `@login_required`; Domänenlogik ist app-frei testbar. Kein neuer Berechtigungsfall. |
| Fehlende/ungültige Eingabe | Kein Nutzer-Input in dieser Logik (Button ohne Payload). Task ohne `title`/leere Felder → Beschreibung ohne diese Zeilen. |
| ID nicht gefunden | Task ohne zugehörigen Kandidaten → Pull (neu anlegen). Kandidat, dessen verknüpfter Task nicht (mehr) in der Task-Liste ist → wird beim Sync ignoriert (kein Push, kein Fehler). |
| Zweiter/doppelter Aufruf (Idempotenz) | Zweiter Sync ohne VMS-Änderung → 0 Pushes, kein API-Call (AC6/AC10). |
| Unzulässiger Statusübergang | Sync verändert Kandidaten-Status nicht. Statuslogik (reconcile) bleibt unberührt. |
| Nebenläufigkeit / Nummernvergabe | Nicht betroffen — keine laufenden Nummern. |
| Externer Dienst nicht erreichbar (Kanboard) | Task-Abruf-Fehler → `{'updated':0,'created':0}`; Einzel-Push-Fehler → geschluckt, weiter (AC8/AC9). |

## 5. Datenmodell

Keine Migration. Keine neuen/geänderten Spalten. `EmailCandidate` bleibt unverändert;
die Felder sind bereits vorhanden. Datumsfelder bleiben Strings (`datum` = ISO/DE), die
Zeitzonen-Umrechnung des Kanboard-Timestamps erfolgt wie bisher über `Europe/Berlin`.

## 6. Schnittstelle

Einstiegspunkt unverändert: `POST /api/emails/sync` → ruft `sync_with_kanboard(user_id)`.
Antwortform unverändert: JSON `{new, kanboard_created, kanboard_updated, last_sync}`.
Kein neues Feld, keine neuen Statuscodes. Berechtigung: Standard-`@login_required`
genügt (geteiltes Modell — jeder eingeloggte Nutzer synchronisiert, so gewollt).

## 7. Architektur

Ausgerichtet an der bestehenden Struktur (`sync_with_kanboard` in
`vms/clients/email_client.py`, Parser in `vms/clients/kanboard_client.py`).

- **Geschäftsregeln (rein, ohne App-Kontext/DB):**
  - `build_task_description(fields: dict) -> str` — neben `parse_description` in
    `kanboard_client.py`, als deren Umkehr; hält das Label-Format an einer Stelle.
  - `kanboard_date_due_to_iso(date_due) -> str` — die heute inline in
    `sync_with_kanboard` liegende Timestamp→ISO-Umrechnung als reine Funktion nach
    `kanboard_client.py` gezogen (Single Source of Truth, auch für den Create-Pfad).
  - `plan_task_push(candidate: dict, task: dict) -> dict | None` — vergleicht den
    VMS-Sollzustand mit dem aktuellen Task und liefert die zu pushenden Felder
    (`title`, `description`, `due_date`, `tags`) oder `None`, wenn nichts abweicht.
    Rein, ohne I/O.
- **Delivery-Schicht (übersetzt nur):** `sync_with_kanboard(user_id)` in
  `email_client.py` — öffnet die DB-Session, lädt Kandidaten, ruft pro Task
  `plan_task_push` und bei Bedarf `kanboard_client.update_task(...)` auf, zählt
  Ergebnisse. Der Create-Zweig für unverknüpfte Tasks bleibt.
- **Berührte Bestandsmodule (minimale Verdrahtung):** `kanboard_client.py` (drei reine
  Funktionen ergänzt), `email_client.py` (`sync_with_kanboard` von Pull auf Push
  umgestellt). Route `email.py` unverändert.
- **Externe Grenzen (im Test gemockt):** `get_leihanfragen_tasks` und `update_task`
  (bzw. `_make_request` via `mock_kanboard`). Keine echte Netz-/Kanboard-Verbindung.

## 8. Nicht im Umfang

- Kein Rückbau von `parse_description` auf eine gemeinsame Label-Konstante (der
  Substring-Parser bleibt; die Label-Dopplung wird als Design-Notiz vermerkt, nicht
  in diesem Feature aufgelöst).
- Kein Löschen/Leeren von Kanboard-Feldern, die in VMS leer sind (kein Clearing).
- Keine Änderung an Status-/Spalten-Reconcile (`reconcile_candidate`), an der
  manuellen Bearbeitung (`update_email_candidate`) oder am IMAP-Mail-Sync.
- Keine UI-Änderung; der Button bleibt „Synchronisierung".

## 9. Offene Fragen

Durch Rückfragen geklärt:
- Mechanik: **aktiv nach Kanboard pushen**, Beschreibung aus VMS-Feldern neu erzeugen.
- Unverknüpfte Tasks: **weiterhin in VMS importieren**.
- Fehlerfall: **best-effort, schlucken & weiter**.

Keine offenen Punkte.
