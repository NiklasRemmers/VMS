# Findings — suspected bugs surfaced while writing tests

Anomalies found during test retrofit. Each row is a **suspected** defect: a test
asserts the domain-correct behaviour and is marked `xfail (strict)`, so the row is
"open" until the code is fixed (then the test xpasses and you remove the marker).

Do not fix application code as part of test writing — log it here, then triage
separately.

Behobene Zeilen werden gelöscht, nicht abgehakt: der zugehörige Test verliert
seinen `xfail`-Marker und bleibt als Regressionstest stehen. Aus Modul 2 sind so
F2–F9 verschwunden (Nummernkollision, stille Unterfakturierung, `success: true`
ohne Persistenz, fehlende Typprüfung, Exception-Leak, Zeitzone, Nummer 0) — die
Regressionstests dazu liegen in `tests/test_invoice_routes.py` und
`tests/test_sequential_number.py`. Kein Finding war das fehlende Ownership-Modell:
dass jeder eingeloggte User alles sieht und bearbeitet, ist in VMS gewollt.

Aus Modul 4 sind zwei `verleih_routes.py`-Findings verschwunden: der falsche
`url_for('public_codes', ...)`-Endpoint (`send_codes` warf `BuildError`, die
gesamte Codes-per-Mail-Funktion war tot) und der fehlende `ACTIVE_STATUSES`-Import
(`get_assignable_loans` warf `NameError`). Beide Einzeiler gefixt, die
Regressionstests dazu (ohne `xfail`) liegen in `tests/test_verleih_routes.py`.

Ebenfalls aus Modul 4 gefixt (`email_routes.py:return_candidate`): die fehlende
Terminal-State-Guard (ein bereits `RETURNED`/`INVOICED`-Vorgang konnte erneut
zurückgegeben werden → jetzt 409). Bewusst nur die echten Endzustände `TERMINAL_STATUSES`
sperren, **nicht** `INVOICE_PENDING`: über dieselbe Route wird dort noch die laufende
Nummer nachgetragen (s. `tests/test_sequential_number.py`). Dazu das fehlende
`or {}`-Fallback bei `request.get_json()` (leerer Body → jetzt geordnete 400 statt 500).
Regressionstests (ohne `xfail`) in `tests/test_email_routes.py`.

Aus Modul 4 gefixt (`security.py`, ehem. Finding #1 — KDF-Divergenz KMS vs.
SECRET_KEY): jedes neue Chiffrat trägt jetzt ein Versionstag (`k1:`/`s1:`), das den
erzeugenden Zweig markiert; `decrypt_value` wählt danach den richtigen Zweig und
übersteht so ein Kippen von `is_kms_available()`. **Bewusst ohne Migration**:
ungetaggte Legacy-Chiffrate in `user_settings` bleiben lesbar (decrypt probiert die
verfügbaren Zweige durch) und werden nicht neu verschlüsselt. Getaggte Chiffrate
weichen NICHT auf den anderen Zweig aus — fehlt der erzeugende Key (z. B. rotierter
SECRET_KEY, verschwundener Master-Key), bleibt es ein sichtbarer `DecryptionError`.
Regressionstests in `tests/test_security.py`
(`test_tagged_ciphertext_survives_kms_becoming_available`,
`test_kms_tagged_ciphertext_is_unreadable_once_master_key_is_gone`).

Aus Modul 6 gefixt (`inventory_routes.py`, ehem. I1–I5): (I1) `create_item`/`update_item`
(sowie `create_bundle`/`update_bundle`) validieren den Body jetzt über den Helper
`_json_body()` — ein nicht-JSON/`null`-Body ergibt eine geordnete 400 statt einer 500.
(I2–I4) ein `DataError` (nicht-numerischer `price`, nicht-integer `count`, zu langer
`name`) wird vor dem generischen Handler abgefangen und als saubere 400 „Ungültige
Eingabewerte" beantwortet; der bisherige `except Exception`-Zweig gibt keine `str(e)`
mehr an den Client, sondern loggt serverseitig via `current_app.logger.exception` und
antwortet generisch (der Zweig selbst ist als `# pragma: no cover` markiert — nur über
einen unerwarteten DB-Fehler erreichbar, der sich ohne Session-Mock nicht auslösen
lässt). (I5) beide Bundle-Routen prüfen die `item_id`s über `_missing_item_ids()` vorab
gegen die DB und liefern bei ungültiger id eine präzise 400 „Unbekannte Item-ID(s): …"
— statt der irreführenden „Name bereits vergeben"-409 aus dem gemeinsamen
`except IntegrityError`. Regressionstests (ohne `xfail`) in `tests/test_inventory_routes.py`.

Aus Modul 6 gefixt (`settings_routes.py`, ehem. S1 — Pfad-Leak): `preview_signature`,
`get_signature_api` und `upload_signature` geben bei einem Krypto-/Konfigurationsfehler
keine rohe Exception-Message mehr an den Client (bisher landete der volle Master-Key-Pfad
`/etc/vms/master.key` aus `FileNotFoundError` im Response-Body bzw. der Flash-Message).
Stattdessen: `current_app.logger.exception(...)` serverseitig + generische Meldung
„Signatur konnte nicht geladen werden" / „Fehler beim Speichern der Unterschrift".
Regressionstest: `tests/test_settings_routes.py::test_preview_signature_does_not_leak_internal_file_path_on_config_error`.

Aus Modul 7 gefixt (`odt_processor.py:_expand_item_rows`, ehem. die zwei offenen
Findings): eine nicht parsbare `quantity`/`price` einer Rechnungs-/Umbuchungs-Position
wird nicht mehr still auf `0`/`0.0` zurückgesetzt (stille Unterfakturierung mit
`0,00 €`-Zeile), sondern wirft jetzt eine `ValueError` mit Positionsbezug — der
Aufrufer bemerkt die kaputten Daten. Ein **fehlender** Key bleibt bewusst eine
gültige `0` (via `.get(..., 0)`); nur explizit nicht-numerische Werte werfen.
`TypeError` (z. B. explizites `None`) wird zu `ValueError` normalisiert.
Regressionstests (ohne `xfail`) in
`tests/test_odt_processor.py::test_expand_item_rows_invalid_quantity_should_not_be_silently_zeroed`
und `::test_expand_item_rows_invalid_price_should_not_be_silently_zeroed`.

## Offene Findings

### Modul `app.py` (Dashboard, Kanboard-Passthrough-Routen, Candidate→Task, Reconcile-Job)

Gefixt (ehem. F-app-1…4, alle vier dieselbe Refactor-Regression — ein
weggefallenes Argument, jeder Aufruf lief in einen `TypeError` → 500 bzw.
durchschlagende Exception): (F-app-1) `app.py:167` gibt `get_leihanfragen_tasks`
jetzt `current_user.id` mit; (F-app-2) `app.py:178` ruft `get_task_details`
mit `(current_user.id, task_id)` in der richtigen Reihenfolge; (F-app-3)
`app.py:309` (`create_task_from_candidate`) ruft `get_candidate_by_id(candidate_id)`
mit nur einem Argument — die 404-Antwort „Kandidat nicht gefunden" ist wieder
erreichbar (kein per-User-Filter nötig, s. Shared-Visibility-Modell); (F-app-4)
`app.py:190` (`generate_pdf`) liest `request.get_json(silent=True)` und antwortet
bei fehlendem/`null`-Body mit einer geordneten 400 statt einer 500. Die
Regressionstests (ohne `xfail`) liegen in `tests/test_app.py`.

Zusätzlich als **Smell** (bewusst kein `xfail`, siehe
`test_index_year_filter_is_a_substring_match_not_exact`): `app.py:143-145`
(`index`) zählt Leihanfragen des laufenden Jahres über
`EmailCandidate.datum.ilike('%<jahr>%')` — ein reiner Substring-Match auf einer
Freitext-Spalte. Ein `datum`-Wert, der die Jahreszahl nur zufällig als Teilstring
enthält (z. B. eine Referenznummer), wird mitgezählt, obwohl er kein Vorgang
dieses Jahres ist. Als Zähler auf dem Dashboard ist das Risiko gering (keine
Geld-/Auth-Auswirkung), aber notiert, falls die Spalte je strenger geprüft werden
soll.

Kein Finding: `app.py:418` in `reconcile_all_rentals` (`except Exception: pass`
pro Kandidat) — verifiziert in
`test_reconcile_all_rentals_does_not_count_a_failed_candidate_as_processed` und
`test_reconcile_all_rentals_counts_only_the_successful_candidate_in_a_mixed_batch`,
dass `processed += 1` innerhalb des `try` *nach* dem Aufruf steht: ein
fehlschlagender Kandidat wird korrekt nicht mitgezählt, während die übrigen
Kandidaten im selben Lauf trotzdem bearbeitet werden. Das Best-Effort-Verhalten
aus dem Docstring hält.

### Modul `email_client.py` (IMAP-Import von Leihanfragen, Kandidaten-CRUD, Kanboard-Sync)

Gefixt (ehem. F-email-1…3): (F-email-1, high) `fetch_emails_for_user`
(email_client.py:296) berechnet `last_sync_utc` und den Alt-Mail-Vergleich jetzt
nur noch innerhalb von `if last_sync:` — der allererste Sync (kein
`EmailSyncState`, `last_sync is None`) wirft keinen `AttributeError` mehr und
liefert wieder alle Anfragen seit Jahresbeginn. (F-email-2, med) ein nicht
interpretierbarer `Date`-Header fällt nicht mehr auf `datetime.now(utc)` zurück,
sondern auf `datetime.min` (UTC, „ältestmöglich") — die Mail unterläuft damit den
`last_sync`-Filter nicht mehr und gilt nicht bei jedem Sync erneut als neu; beim
allerersten Sync wird sie einmalig importiert und danach über `email_id` dedupt.
(F-email-3, med) `get_archived_candidates` liefert bei einem nicht parsbaren
`date_filter` jetzt ein leeres Ergebnis (`q.filter(false())`) statt still das
ganze Archiv. Regressionstests (ohne `xfail`) in `tests/test_email_client.py`.

Kein Finding (bewusst gepinnt, kein `xfail`): der bare `except: []` bei Tags
(email_client.py:426/501/788) — derselbe Display-Fallback wie
`format_money_de` (siehe unten). `tags` ist eine JSONB-Spalte, die im
Normalbetrieb nur über die ORM als echte Python-Liste geschrieben wird; ein
als String gespeicherter/kaputter Wert kann nur durch einen direkten,
ORM-fremden Schreibzugriff entstehen. Die Rohdaten bleiben in der DB
unverändert, nur die API-Antwort degradiert auf `[]`. Getestet in
`tests/test_email_client.py::test_get_candidates_malformed_json_tags_fall_back_to_empty_list`
u. a. Ebenfalls kein Finding: `sync_with_kanboard`s
Swallow-und-Nullen-Rückgabe bei einem fehlschlagenden
`get_leihanfragen_tasks` (L522-526) — die Kanboard-Verbindung ist ein bekannt
unzuverlässiger externer Dienst, und der Aufrufer zeigt ohnehin nur die
Zähler an; ein harter Fehler hier würde nur den Sync-Button crashen lassen,
ohne einen anderen Recovery-Pfad zu bieten.

_Keine weiteren offenen Findings._ Die folgenden Absätze dokumentieren bewusst belassenes
Verhalten (kein `xfail`), keine offenen Bugs.

Notiz zu `odt_processor.py:430-438` (`format_money_de`): dieselbe Art
Except-Fallback (`None`/nicht-numerisch → `0.0`), aber bewusst **nicht** als
Finding gewertet — die Funktion ist ein generischer Anzeige-Formatter, der von
vielen Stellen mit potenziell fehlenden Werten aufgerufen wird (nicht
spezifisch für eine einzelne Rechnungsposition), und "leer/0,00 anzeigen statt
crashen" ist für reine Darstellungslogik eine vertretbare Defensivmaßnahme.
Gepinnt (kein `xfail`) in `tests/test_odt_processor.py::test_format_money_de_none_falls_back_to_zero`
und `::test_format_money_de_non_numeric_string_falls_back_to_zero`.

Notiz zu `update_bundle`/`create_bundle`: ein `items`-Eintrag mit falsy `item_id`
(`0`, `None`, fehlend) wird über `if item_id:` still übersprungen statt einen Fehler
zu liefern (`tests/test_inventory_routes.py::test_update_bundle_items_entry_with_falsy_item_id_is_silently_skipped`,
`::test_create_bundle_items_entry_with_falsy_item_id_is_silently_skipped`). Als
bewusstes/dokumentiertes Verhalten belassen (kein `xfail`) — plausibel als Absicht
("kein Item angegeben → nichts anlegen"), aber ohne Fehlermeldung an den Client kann
ein Tippfehler im Payload unbemerkt bleiben.

Notiz zu `settings_routes.py` (Modul 6): die Asymmetrie "leeres Passwort/Token im
Update-Pfad behält das alte Chiffrat, leeres Passwort/Token im Create-Pfad wird mit
400 abgelehnt" (`update_email_manual` L89-92, `update_kanboard` L156-157) ist
bewusstes/dokumentiertes Verhalten, kein Finding — beide Seiten sind explizit
gepinnt: `tests/test_settings_routes.py::test_email_settings_update_keeps_old_ciphertext_when_password_blank`,
`::test_email_settings_create_missing_password_returns_400`,
`::test_kanboard_settings_update_keeps_old_token_when_omitted`,
`::test_kanboard_settings_create_missing_token_returns_400`.

Ebenfalls zu Modul 6: `settings_page`s `has_signature`-Lookup (L263-270) umschließt
eine zweite `with get_session() as s: ...` mit einem blanken
`except Exception as e: print(...)`, der jeden Fehler schluckt und still auf
`has_signature = False` zurückfällt. Bewusst ungetestet gelassen — das Erreichen
würde ein Mocken von `get_session`/der Session erfordern, was der Testing-Skill
verbietet (nur die drei benannten externen Grenzen dürfen gemockt werden). Kein
Test hinzugefügt; hier nur als theoretisches Silent-Swallow dokumentiert
(`except Exception: print()` verliert den Fehler komplett, nicht einmal über
`current_app.logger` geloggt).

Modul 6 (`template_store.py`/`template_routes.py`, Dokumentvorlagen-Store): keine
neuen `xfail`-Findings — alle Verhaltensweisen unten sind Smells/Beobachtungen,
nicht Bugs; die Tests pinnen bewusst das jetzige (nachvollziehbare) Verhalten.

- **Side-effecting GET**: `GET /api/templates` (`api_list_templates` →
  `template_store.list_versions` → `ensure_seeded`) fügt beim allerersten Aufruf
  pro Typ eine INSERT-Zeile (Version 1 aus dem mitgelieferten Bundle) ein. Ein
  reiner Lesezugriff ist damit nicht idempotent/seiteneffektfrei — ein zweiter
  identischer GET liefert dieselben Daten, aber der erste hat die DB verändert.
  Nachvollziehbar als "lazy seed on first use", aber ungewöhnlich für eine
  GET-Route. Getestet in `tests/test_template_routes.py::test_list_templates_first_call_seeds_all_three_types`
  und `tests/test_template_store.py::test_list_versions_seeds_and_returns_newest_first`.
- **Nahezu tote Branch**: `store_new_version`s `next_version = (highest.version + 1)
  if highest else 1` (template_store.py:205) — der `else 1`-Zweig setzt voraus,
  dass für den angefragten `template_type` noch keine einzige Version existiert.
  Da die Funktion selbst zuerst `ensure_seeded(template_type)` aufruft, ist das
  für jeden der drei bekannten Typen (`leihvertrag`/`rechnung`/`umbuchung`)
  unerreichbar — `ensure_seeded` legt garantiert Version 1 an, bevor die
  `highest`-Abfrage läuft. Anders als `load_template`/`validate_template`
  validiert `store_new_version` den `template_type` aber selbst nicht gegen
  `BUNDLED_TEMPLATES`; bei einem direkten (Route-umgehenden) Aufruf mit einem
  unbekannten Typ wäre der Zweig also doch erreichbar. Kein `# pragma: no cover`
  ergänzt: coverage.py trackt den ternären Ausdruck ohnehin nicht als eigene
  Branch (beide Coverage-Läufe zeigen 100 % Branch-Coverage für die Datei, ohne
  dass ein Test den `else`-Zweig je auslöst) — eine Pragma-Zeile wäre hier nur
  Dokumentation, keine Coverage-Notwendigkeit.
- **`_probe_render`s breiter `except Exception as e: return False, str(e)`**
  (template_store.py:348-349) kollabiert jede Ursache eines fehlgeschlagenen
  Testdrucks (kaputtes Template, `process_odt_template`-Bug, LibreOffice-Crash,
  …) auf eine einzelne String-Message, die 1:1 im `errors`-Array an den Client
  geht. Bewusst so benannt in der Aufgabenbeschreibung als Design-Entscheidung
  (der Nutzer soll *irgendeinen* Grund sehen, nicht dass der Prozess abstürzt);
  serverseitig wird der Traceback dabei nicht geloggt, was eine echte Diagnose
  erschwert, falls die Ursache kein Template-Fehler, sondern z. B. ein
  LibreOffice-Ausfall ist. Kein `xfail` — reine Beobachtung.
- **Test-Infrastruktur-Falle**: `template_store.py` importiert `convert_to_pdf`
  mit `from odt_processor import convert_to_pdf` auf Modulebene. Die geteilte
  `no_libreoffice`-Fixture patcht `odt_processor.convert_to_pdf` (das Attribut
  auf dem *odt_processor*-Modul) — das ändert die bereits gebundene Kopie des
  Namens in `template_store`s eigenem Namensraum nicht (verifiziert: ein Patch
  auf `odt_processor.convert_to_pdf` lässt `template_store.convert_to_pdf`
  unverändert `is`-identisch mit der ungepatchten Originalfunktion). Für
  `invoice_routes.py` funktioniert dieselbe Fixture nur, weil dort der Import
  lokal in der Funktion steht (`from odt_processor import ... convert_to_pdf ...`
  innerhalb von `api_send_invoice`), also zur Aufrufzeit *nach* dem Patch läuft.
  `app.py`s `generate_pdf()` hat denselben Modul-Import wie `template_store.py`
  (`from odt_processor import convert_to_pdf, process_odt_template` ganz oben in
  app.py) und dürfte über dieselbe Falle stolpern, falls dort je mit
  `no_libreoffice` statt einem direkten `mocker.patch("app.convert_to_pdf", ...)`
  getestet würde. `tests/test_template_store.py` und
  `tests/test_template_routes.py` patchen deshalb `template_store.convert_to_pdf`
  direkt (eigene `stub_convert_to_pdf`-Fixture) statt sich auf `no_libreoffice`
  zu verlassen.

Severity: **high** = correctness/security (money, auth, data loss) · **med** =
wrong-but-contained · **low** = cosmetic/edge. Triage high first.
