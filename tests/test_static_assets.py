"""Tests für die Auslieferung statischer Dateien.

Nginx liefert /static/ mit `expires 7d; Cache-Control: public, immutable`
(ops/deploy/nginx.conf). Ein Browser holt eine so ausgelieferte Datei bis zum
Ablauf **nicht** neu -- auch nicht per Revalidierung. Damit ein Fix in einer
JS-/CSS-Datei die Nutzer überhaupt erreicht, muss die URL im Template den
Dateiinhalt widerspiegeln: gleicher Inhalt -> gleiche URL (Cache greift),
geänderter Inhalt -> neue URL (Cache wird umgangen).
"""
import pytest

from vms.infra.assets import asset_url, clear_cache


@pytest.fixture()
def static_dir(tmp_path):
    clear_cache()
    yield tmp_path
    clear_cache()


# ---------------------------------------------------------------------------
# asset_url -- Versionskennung aus dem Dateiinhalt
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_asset_url_appends_version_query(static_dir):
    (static_dir / "widget.js").write_text("console.log(1)", encoding="utf-8")

    url = asset_url("widget.js", str(static_dir))

    assert url.startswith("/static/widget.js?v=")


@pytest.mark.unit
def test_asset_url_is_stable_for_unchanged_file(static_dir):
    (static_dir / "widget.js").write_text("console.log(1)", encoding="utf-8")

    assert asset_url("widget.js", str(static_dir)) == asset_url("widget.js", str(static_dir))


@pytest.mark.unit
def test_asset_url_changes_when_file_content_changes(static_dir):
    asset = static_dir / "widget.js"
    asset.write_text("console.log('alt')", encoding="utf-8")

    before = asset_url("widget.js", str(static_dir))
    asset.write_text("console.log('neu, deutlich länger')", encoding="utf-8")
    after = asset_url("widget.js", str(static_dir))

    assert before != after


@pytest.mark.unit
def test_asset_url_without_file_still_yields_usable_url(static_dir):
    """Eine fehlende Datei darf das Rendern der Seite nicht sprengen."""
    assert asset_url("gibt-es-nicht.js", str(static_dir)) == "/static/gibt-es-nicht.js"


@pytest.mark.unit
def test_asset_url_survives_unreadable_path(static_dir):
    """stat() gelingt, open() nicht -- auch dann muss eine URL herauskommen."""
    (static_dir / "signatures").mkdir()

    assert asset_url("signatures", str(static_dir)) == "/static/signatures"


# ---------------------------------------------------------------------------
# Templates -- jede eingebundene statische Datei trägt eine Version
# ---------------------------------------------------------------------------

@pytest.mark.route
@pytest.mark.parametrize("path", ["/leihvertrag", "/emails", "/inventory", "/invoices"])
def test_rendered_page_versions_every_static_reference(auth_client, path):
    """Kein `src="/static/..."` ohne `?v=` -- sonst bleibt der Fix im Cache."""
    import re

    resp = auth_client.get(path)

    assert resp.status_code == 200
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', resp.data.decode())
    assert refs, f"{path} bindet keine statischen Dateien ein -- Test greift ins Leere"
    assert [r for r in refs if "?v=" not in r] == []
