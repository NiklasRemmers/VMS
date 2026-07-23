# Spec: Rechnung/Umbuchung nur herunterladen (kein Mailversand)

Status: implemented · Datum: 2026-07-23

## 1. Zweck

Aktuell erzeugt der Rechnungstab beim Klick auf „Rechnung verschicken" /
„Umbuchung verschicken" ein PDF und **verschickt es sofort per E-Mail** an den
Empfänger (`POST /api/invoices/send` → `auth.send_email_with_attachment`). Der
automatische Mailversand soll komplett entfallen: Rechnungen und Umbuchungen
werden nur noch als PDF **heruntergeladen**. Der Bearbeiter versendet das Dokument
danach selbst außerhalb von VMS. Alles, was ausschließlich für den Mailversand
existierte, wird entfernt.

## 2. Domänenregeln

Die Wahrheitsquelle, gegen die getestet wird:

- **Kein Mailversand.** Beim Erstellen einer Rechnung/Umbuchung geht keine E-Mail
  raus. Das erzeugte PDF wird als HTTP-Download (`Content-Disposition: attachment`)
  an den Browser zurückgegeben.
- **Der Vorgang wird ansonsten wie bisher abgeschlossen** (Entscheidung des
  Nutzers, Freigabe-Tor Phase 0): verbindliche Vergabe der laufenden Nummer unter
  Sperre (kollisionsfrei, Zähler +1), `status` → `invoiced`, `nummer_typ` und
  `laufende_nummer` am Kandidaten gesetzt, Kanboard-Task schließen (best effort).
- **Empfänger-E-Mail und Mail-Text entfallen vollständig** — im Formular, im
  Request-Payload und im Backend. Es gibt keinen Empfänger mehr, weil nichts
  verschickt wird.
- **Rechnungsanschrift (`adresse`) und Kostenstelle bleiben.** `adresse` füllt
  weiterhin `#ADRESSE#` im Rechnungs-PDF; die Umbuchung bleibt intern adressiert
  und trägt nur `#KOSTENSTELLE#` (siehe bestehendes Verhalten, unverändert).
- **Gesamtbetrag wird serverseitig aus den Posten neu berechnet** (Client-Wert
  wird nicht vertraut). Ein Posten mit unlesbarem Preis/Menge → Fehler, nichts
  wird persistiert und kein PDF geht raus.
- **Reihenfolge / Fehlersicherheit:** Die Nummer wird VOR der PDF-Erzeugung
  reserviert. Schlägt die PDF-Erzeugung fehl, wird die Nummer wieder freigegeben
  (keine Lücke im Nummernkreis), der Vorgang bleibt unverändert. Persistenz von
  Status/Nummer erfolgt erst NACH erfolgreicher PDF-Erzeugung; danach wird das PDF
  als Download zurückgegeben.
- **Aufgeräumt wird auch der tote Code:** `auth.send_email_with_attachment` hat
  nach dieser Änderung keinen Aufrufer mehr (verifiziert: nur `invoice_routes.py`
  nutzt sie) und wird entfernt.

Unverändert gegenüber heute (nur zur Klarheit):
- laufende Nummer ist typgebunden (`rechnung`/`umbuchung`-Zähler sind unabhängig).
- `contract_created` wird beim Fakturieren NICHT gesetzt; fehlt ein Leihvertrag,
  wird eine Warnung geloggt.
- Datums-Platzhalter (`#HEUTE#`, `#JAHR#`) nutzen lokale Zeitzone.
- Geteilte Sichtbarkeit: jeder eingeloggte User darf jeden Kandidaten fakturieren.

## 3. Akzeptanzkriterien

| # | Kriterium | Testname |
|---|-----------|----------|
| 1 | Erfolgreiche Rechnung liefert das PDF als Download (200, `application/pdf`, `Content-Disposition: attachment` mit Dateiname) und persistiert `status='invoiced'`, `laufende_nummer`, `nummer_typ`; Zähler erhöht | `test_download_invoice_rechnung_happy_path_returns_pdf_and_persists` |
| 2 | Es geht KEINE E-Mail raus (Postfach leer) | `test_download_invoice_sends_no_mail` |
| 3 | Die vergebene Nummer steht im Antwort-Header `X-Laufende-Nummer` | `test_download_invoice_returns_laufende_nummer_header` |
| 4 | Umbuchung setzt `#KOSTENSTELLE#`, ohne `#VORNAME NACHNAME#`/`#ADRESSE#`; `nummer_typ='umbuchung'` | `test_download_invoice_umbuchung_sets_kostenstelle_and_omits_internal_address` |
| 5 | Dateiname nutzt den Veranstaltungsnamen, wenn vorhanden | `test_download_invoice_filename_uses_veranstaltung_when_present` |
| 6 | Ohne Veranstaltungsnamen nutzt der Dateiname Nummer + Name | `test_download_invoice_filename_falls_back_to_number_and_name` |
| 7 | Ungültiger `nummer_typ` → 400, nichts persistiert | `test_download_invoice_rejects_invalid_nummer_typ` |
| 8 | Leere Postenliste → 400 | `test_download_invoice_rejects_empty_items` |
| 9 | `items` kein Array → 400 | `test_download_invoice_rejects_items_not_a_list` |
| 10 | Umbuchung ohne Kostenstelle → 400 | `test_download_invoice_umbuchung_requires_kostenstelle` |
| 11 | Unbekannter Kandidat → 404 | `test_download_invoice_unknown_candidate_returns_404` |
| 12 | Nicht authentifiziert → 401 | `test_download_invoice_anonymous_is_unauthorized` |
| 13 | Unlesbarer Preis/Menge → 400, nichts persistiert, kein Zähler | `test_download_invoice_rejects_unparsable_item_price` |
| 14 | Ohne `laufende_nummer` vergibt der Server selbst (Normalfall) | `test_download_invoice_allocates_when_laufende_nummer_omitted` |
| 15 | Zähler wird aus bestehendem Wert erhöht | `test_download_invoice_advances_sequential_counter_from_existing_value` |
| 16 | Kleinere Nummer senkt den Zähler nicht | `test_download_invoice_does_not_lower_counter_for_smaller_number` |
| 17 | Nicht-numerische Nummer → 400, nichts persistiert, kein Zähler | `test_download_invoice_rejects_non_numeric_laufende_nummer` |
| 18 | Nummer 0 → 400 | `test_download_invoice_rejects_laufende_nummer_zero` |
| 19 | PDF-Erzeugung schlägt fehl → Nummer freigegeben, 500, Vorgang unverändert | `test_download_invoice_pdf_failure_releases_number_and_leaves_candidate_unchanged` |
| 20 | Kanboard-Abgleich schlägt fehl → trotzdem 200 + PDF, `status='invoiced'` | `test_download_invoice_survives_kanboard_reconcile_failure` |
| 21 | `contract_created` bleibt unberührt (True bleibt True) | `test_download_invoice_keeps_an_existing_contract_flag` |
| 22 | Fehlender Leihvertrag wird geloggt (Warnung), aber nicht blockiert | `test_download_invoice_warns_when_no_contract_was_ever_created` |
| 23 | Datums-Platzhalter nutzen lokale Zeitzone | `test_download_invoice_uses_local_timezone_for_date_placeholders` |
| 24 | Kandidat eines anderen Users wird akzeptiert (geteilte Sichtbarkeit) | `test_download_invoice_accepts_candidate_of_another_user` |
| 25 | `rechnung`- und `umbuchung`-Zähler bleiben unabhängig | `test_download_invoice_umbuchung_and_rechnung_counters_stay_independent` (test_sequential_number.py) |
| 26 | `auth.send_email_with_attachment` existiert nicht mehr | `test_send_email_with_attachment_is_removed` |

## 4. Fehlerfälle & Randbedingungen

| Fall | Erwartetes Verhalten |
|------|----------------------|
| Nicht authentifiziert | 401 JSON (via `unauthorized_handler`), keine Wirkung |
| Fehlende/ungültige Eingabe (Typ, Posten, Kostenstelle, Preis) | 400, nichts persistiert, keine Nummer vergeben |
| Kandidat-ID nicht gefunden | 404, nichts persistiert |
| Zweiter/doppelter Aufruf | Kein spezieller Idempotenz-Schutz gefordert; jeder erfolgreiche Aufruf vergibt eine neue Nummer und setzt erneut `invoiced`. (Kandidat verschwindet nach dem ersten Download aus der offenen Liste; ein zweiter Aufruf ist im UI nicht vorgesehen.) — Verhalten unverändert gegenüber heute. |
| Unzulässiger Statusübergang | Kein neuer Guard gefordert (bestehendes Verhalten übernommen) |
| Nebenläufigkeit / Nummernvergabe | Reservierung unter Sperre via `claim_sequential_number` (unverändert); bei PDF-Fehler `release_sequential_number` |
| PDF-Erzeugung (load_template/process_odt/convert_to_pdf) schlägt fehl | Nummer freigeben, 500 „Dokument konnte nicht erstellt werden", Vorgang unverändert; keine internen Details in der Antwort |
| Kanboard nicht erreichbar | best effort; Download gelingt trotzdem (200 + PDF) |
| Kandidat verschwindet zwischen PDF-Bau und Persistenz | Nummer freigeben, 500, kein PDF ausgeliefert (nichts ging raus — sauberer als der alte „verschickt aber nicht gespeichert"-Zustand) |

## 5. Datenmodell

Keine Schema-Änderung. `EmailCandidate.status`, `.laufende_nummer`, `.nummer_typ`
und `SequentialNumber` werden wie bisher genutzt. Keine Migration.

## 6. Schnittstelle

- Endpunkt (umbenannt, da „send" nicht mehr zutrifft):
  `POST /api/invoices/download` (`@login_required`).
- Request-JSON (E-Mail-Felder entfernt):
  `candidate_id`, `nummer_typ` (`rechnung`|`umbuchung`), `laufende_nummer`
  (optional), `adresse` (nur Rechnung), `kostenstelle` (nur Umbuchung), `items`
  (Liste `{name, price, quantity, unit}`).
  **Entfernt:** `email`, `mail_text`.
- Erfolgs-Antwort: **200**, Body = PDF-Bytes, `Content-Type: application/pdf`,
  `Content-Disposition: attachment; filename="…"`, Header `X-Laufende-Nummer: <n>`.
- Fehler-Antwort: JSON `{error: …}` mit 400/404/409/500 (wie oben).
- Berechtigung: Standard-`@login_required`, keine Ownership-Prüfung (geteilte
  Sichtbarkeit ist in VMS gewollt).

## 7. Architektur

An der bestehenden Struktur ausgerichtet (der Rechnungsvorgang lebt heute komplett
in `invoice_routes.py`; `invoice_routes.py` ist mit ~275 Zeilen nicht
überdimensioniert):

- **Delivery-Schicht:** `invoice_routes.api_download_invoice` (ersetzt
  `api_send_invoice`). Übersetzt Request → ruft Domänenlogik → liefert PDF-Download.
  Die Route öffnet Sessions/Temp-Verzeichnis am Rand (unverändert).
- **Geschäftsregel (rein, ohne App-Kontext/DB):** Der Aufbau des
  `replacements`-Dicts inkl. Gesamtbetrag ist reine Logik. Sie ist heute in der
  Route inline und wird über die Route getestet. Minimalprinzip: keine spekulative
  Extraktion — nur wenn Phase 3 (Refactor) es sinnvoll macht, wird eine reine
  Helferfunktion herausgezogen; die Tests bleiben verhaltensbasiert.
- **Berührte bestehende Module (minimale Verdrahtung/Rückbau):**
  - `invoice_routes.py`: Route umbauen (Download statt Mail), Import und Felder
    `email`/`mail_text` entfernen.
  - `auth.py`: `send_email_with_attachment` entfernen (toter Code).
  - `templates/invoices.html`: E-Mail-Feld + Mail-Text-Feld entfernen,
    Button „…verschicken" → „…herunterladen", `sendInvoice()` → Blob-Download.
  - `tests/test_invoice_routes.py`, `tests/test_sequential_number.py`: an die
    neue Semantik anpassen (kein Mail-Stub, PDF-Antwort, Header statt JSON-Nummer).
- **Externe Grenzen (im Test gemockt):** `odt_processor.convert_to_pdf` via
  `no_libreoffice`; `process_odt_template`/`load_template` gestubbt; Kanboard via
  `mock_kanboard`. Mail-Grenze entfällt.

## 8. Nicht im Umfang

- Kein manueller „per Mail verschicken"-Button als Ersatz. Versand passiert
  vollständig außerhalb von VMS.
- Keine Änderung an der Rückgabe-/Return-Nummernvergabe in `email_routes.py`
  (nutzt `laufende_nummer` über eine andere Route, unberührt).
- Keine Idempotenz-/Doppelklick-Sperre (nicht gefordert, Verhalten wie heute).
- Kein Speichern des PDFs serverseitig; es wird nur ausgeliefert.
- `send_plain_email`/`send_invitation_email` (auth.py) bleiben — sie werden von
  anderen Features genutzt und haben nichts mit dem Rechnungsversand zu tun.

## 9. Offene Fragen

Die beiden fachlichen Kernfragen wurden am Freigabe-Tor Phase 0 geklärt:
1. Download schließt den Vorgang ab wie bisher (nur ohne Mail). ✔
2. E-Mail- und Mail-Text-Feld werden entfernt. ✔

Verbleibend zur Bestätigung mit der Spec:
- **Endpunkt-Umbenennung** `/api/invoices/send` → `/api/invoices/download`: sinnvoll,
  weil „send" die Semantik nicht mehr trifft. Falls die alte URL aus irgendeinem
  Grund stabil bleiben soll, bitte melden — sonst wird umbenannt.
- **`X-Laufende-Nummer`-Header**: als Weg, die vergebene Nummer trotz PDF-Body an
  Frontend/Tests zurückzugeben. Einverstanden?
