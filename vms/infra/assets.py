"""Versionierte URLs für statische Dateien (Cache-Busting).

Nginx liefert /static/ mit `expires 7d; Cache-Control: public, immutable`
(ops/deploy/nginx.conf). `immutable` heißt: der Browser fragt bis zum Ablauf
nicht einmal nach, ob die Datei sich geändert hat. Eine unveränderte URL bedeutet
deshalb, dass ein Deploy die Nutzer bis zu sieben Tage lang nicht erreicht.

Die Versionskennung ist ein Hash des Dateiinhalts: gleicher Inhalt -> gleiche URL
(der Cache greift weiter), geänderter Inhalt -> neue URL (der Cache wird
umgangen). Ein Zufalls- oder Zeitstempelwert pro Request würde das Caching
stattdessen komplett abschalten.
"""
import hashlib
import os

# path -> ((mtime_ns, size), digest). Der stat-Schlüssel hält den Hash aktuell,
# ohne die Datei bei jedem Request zu lesen.
_versions: dict[str, tuple[tuple[int, int], str]] = {}


def clear_cache() -> None:
    """Verwirft die gecachten Versionskennungen (Tests)."""
    _versions.clear()


def _version(path: str) -> str | None:
    """Kurzer Inhalts-Hash der Datei; None, wenn sie nicht lesbar ist."""
    try:
        st = os.stat(path)
    except OSError:
        return None

    key = (st.st_mtime_ns, st.st_size)
    cached = _versions.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]

    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                digest.update(chunk)
    except OSError:
        return None

    short = digest.hexdigest()[:10]
    _versions[path] = (key, short)
    return short


def asset_url(filename: str, static_dir: str) -> str:
    """URL für eine Datei unter static/, mit Inhalts-Version als `?v=`.

    Fehlt die Datei, wird die URL ohne Version geliefert -- eine falsch
    geschriebene Referenz soll als 404 auffallen, nicht das Rendern der ganzen
    Seite abbrechen.
    """
    version = _version(os.path.join(static_dir, filename))
    return f"/static/{filename}?v={version}" if version else f"/static/{filename}"
