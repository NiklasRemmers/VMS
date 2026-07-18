"""
Template Store Module
Versioned storage, retrieval and validation of the ODT document templates.

Templates used to be plain files baked into the image. They are now rows in
``document_templates`` so they can be replaced from the UI, with every previous
version kept for rollback. The bundled files still ship with the image and are
used to seed version 1 on first use.
"""

import hashlib
import os
import re
import shutil
import tempfile
import zipfile

from kms import encrypt_binary, decrypt_binary
from database import get_session
from models import DocumentTemplate
from odt_processor import (
    SIGNATURE_BLOCK_PLACEHOLDER,
    convert_to_pdf,
    normalize_fragmented_placeholders,
    process_odt_template,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Bundled fallbacks, used to seed version 1 when a type has no stored template.
BUNDLED_TEMPLATES = {
    'leihvertrag': os.path.join(BASE_DIR, 'template.odt'),
    'rechnung': os.path.join(BASE_DIR, 'template_rechnung.odt'),
    'umbuchung': os.path.join(BASE_DIR, 'template_umbuchung.odt'),
}

TEMPLATE_LABELS = {
    'leihvertrag': 'Leihvertrag',
    'rechnung': 'Rechnung',
    'umbuchung': 'Umbuchung',
}

ODT_MIMETYPE = b'application/vnd.oasis.opendocument.text'

# Placeholders a template of each type must contain. Sourced from the actual
# call sites: app.py generate_pdf() and invoice_routes.py api_send_invoice().
REQUIRED_PLACEHOLDERS = {
    'leihvertrag': [
        '#VORNAME NACHNAME#', '#PRIVATANSCHRIFT#', '#RECHNUNGSANSCHRIFT#',
        '#ABHOLDATUM#', '#RÜCKGABEDATUM#', '#VERANSTALTUNGSNAME#',
        '#VERANSTALTUNGSDATUM#', '#VERANSTALTUNGSORT#', '#MATERIAL#',
        '#HEUTE#', '#VERLEIHER#', SIGNATURE_BLOCK_PLACEHOLDER,
    ],
    'rechnung': [
        '#NUMMER#', '#JAHR#', '#HEUTE#', '#GESAMTPREIS#', '#VERLEIHER#',
        '#VORNAME NACHNAME#', '#ADRESSE#', '#VERANSTALTUNG#',
        '#ARTIKEL#', '#MENGE#', '#STÜCKPREIS#', '#GESAMTPREIS_POS#',
    ],
    # An Umbuchung is addressed internally, so it deliberately carries no
    # recipient name or address — those two must not be required here.
    'umbuchung': [
        '#NUMMER#', '#JAHR#', '#HEUTE#', '#GESAMTPREIS#', '#VERLEIHER#',
        '#VERANSTALTUNG#', '#KOSTENSTELLE#',
        '#ARTIKEL#', '#MENGE#', '#STÜCKPREIS#', '#GESAMTPREIS_POS#',
    ],
}

# Placeholders that are accepted but not required, so they don't trip the
# "unknown placeholder" warning.
OPTIONAL_PLACEHOLDERS = {
    '#UNTERSCHRIFT#', '#ZUSAMMEN_START#', '#ZUSAMMEN_ENDE#',
    # Accepted on an Umbuchung if someone does want to address it outward.
    '#VORNAME NACHNAME#', '#ADRESSE#',
}


# ─── Reading ───

def _read_content_xml(content: bytes) -> str:
    """Return the normalized content.xml of an ODT byte blob."""
    with zipfile.ZipFile(_as_stream(content)) as z:
        xml = z.read('content.xml').decode('utf-8')
    # LibreOffice happily splits a placeholder across several <text:span>s;
    # without normalizing first, every placeholder check would give false alarms.
    return normalize_fragmented_placeholders(xml)


def _as_stream(content: bytes):
    import io
    return io.BytesIO(content)


def get_active(session, template_type: str):
    return (session.query(DocumentTemplate)
            .filter_by(template_type=template_type, is_active=True)
            .first())


def list_versions(template_type: str):
    """All stored versions of a type, newest first.

    Seeds the bundled template first if nothing is stored yet, so the shipped
    baseline is always visible in the UI and stays available as a rollback
    target even if the very first action is an upload.
    """
    ensure_seeded(template_type)
    with get_session() as s:
        rows = (s.query(DocumentTemplate)
                .filter_by(template_type=template_type)
                .order_by(DocumentTemplate.version.desc())
                .all())
        return [r.to_dict() for r in rows]


def get_content(template_id: int):
    """Return (filename, plaintext bytes) for one stored version."""
    with get_session() as s:
        row = s.query(DocumentTemplate).filter_by(id=template_id).first()
        if not row:
            return None, None
        return row.filename, decrypt_binary(row.encrypted_content)


def ensure_seeded(template_type: str):
    """Store the bundled template as version 1 if the type has no versions yet."""
    if template_type not in BUNDLED_TEMPLATES:
        return
    with get_session() as s:
        exists = (s.query(DocumentTemplate)
                  .filter_by(template_type=template_type)
                  .first() is not None)
    if not exists:
        _seed_from_bundle(template_type)


def load_template(template_type: str, target_dir: str) -> str:
    """Write the active template of a type into target_dir and return its path.

    Seeds version 1 from the bundled file when nothing is stored yet, so a fresh
    deployment works without a manual import step.
    """
    if template_type not in BUNDLED_TEMPLATES:
        raise ValueError(f'Unbekannter Template-Typ: {template_type}')

    with get_session() as s:
        row = get_active(s, template_type)
        if row:
            content = decrypt_binary(row.encrypted_content)
            filename = row.filename
        else:
            content = None

    if content is None:
        content = _seed_from_bundle(template_type)
        filename = os.path.basename(BUNDLED_TEMPLATES[template_type])

    path = os.path.join(target_dir, filename)
    with open(path, 'wb') as f:
        f.write(content)
    return path


def _seed_from_bundle(template_type: str) -> bytes:
    """Store the bundled file as version 1 and return its bytes."""
    bundled_path = BUNDLED_TEMPLATES[template_type]
    with open(bundled_path, 'rb') as f:
        content = f.read()

    with get_session() as s:
        # Re-check inside the transaction: two workers can race on first request.
        # Guard on *any* version, not just an active one — seeding a second
        # version 1 would violate the (template_type, version) constraint.
        existing = (s.query(DocumentTemplate)
                    .filter_by(template_type=template_type)
                    .order_by(DocumentTemplate.version.desc())
                    .first())
        if existing:
            active = get_active(s, template_type) or existing
            return decrypt_binary(active.encrypted_content)
        s.add(DocumentTemplate(
            template_type=template_type,
            version=1,
            filename=os.path.basename(bundled_path),
            encrypted_content=encrypt_binary(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            is_active=True,
            note='Automatisch aus der mitgelieferten Vorlage übernommen',
        ))
    return content


# ─── Writing ───

def store_new_version(template_type: str, filename: str, content: bytes,
                      user_id: int = None, note: str = None) -> dict:
    """Store content as a new version and make it the active one."""
    # Keep the shipped baseline as version 1 even if an upload comes first.
    ensure_seeded(template_type)
    with get_session() as s:
        highest = (s.query(DocumentTemplate)
                   .filter_by(template_type=template_type)
                   .order_by(DocumentTemplate.version.desc())
                   .first())
        next_version = (highest.version + 1) if highest else 1

        s.query(DocumentTemplate).filter_by(
            template_type=template_type, is_active=True
        ).update({'is_active': False})

        row = DocumentTemplate(
            template_type=template_type,
            version=next_version,
            filename=filename,
            encrypted_content=encrypt_binary(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            is_active=True,
            note=note or None,
            uploaded_by=user_id,
        )
        s.add(row)
        s.flush()
        return row.to_dict()


def activate(template_id: int) -> dict:
    """Make one stored version the active one for its type (rollback)."""
    with get_session() as s:
        row = s.query(DocumentTemplate).filter_by(id=template_id).first()
        if not row:
            return None
        s.query(DocumentTemplate).filter_by(
            template_type=row.template_type, is_active=True
        ).update({'is_active': False})
        row.is_active = True
        s.flush()
        return row.to_dict()


def delete_version(template_id: int):
    """Delete a stored version. Returns (ok, error)."""
    with get_session() as s:
        row = s.query(DocumentTemplate).filter_by(id=template_id).first()
        if not row:
            return False, 'Version nicht gefunden'
        if row.is_active:
            return False, 'Die aktive Version kann nicht gelöscht werden'
        s.delete(row)
        return True, None


# ─── Validation ───

def validate_template(content: bytes, template_type: str):
    """Check an uploaded ODT before it is allowed to become active.

    Returns (ok, errors, warnings). Errors block the upload, warnings do not.
    """
    errors = []
    warnings = []

    if template_type not in BUNDLED_TEMPLATES:
        return False, [f'Unbekannter Template-Typ: {template_type}'], []

    # 1. Structure
    try:
        with zipfile.ZipFile(_as_stream(content)) as z:
            names = z.namelist()
            if 'content.xml' not in names:
                errors.append('Die Datei enthält keine content.xml — das ist keine ODT-Datei.')
            mimetype = z.read('mimetype') if 'mimetype' in names else b''
            if mimetype.strip() != ODT_MIMETYPE:
                errors.append(
                    'Die Datei ist kein ODF-Textdokument. Bitte in LibreOffice Writer '
                    'als .odt speichern (nicht .docx, .odg oder .ods).'
                )
    except zipfile.BadZipFile:
        return False, ['Die Datei ist beschädigt oder keine ODT-Datei.'], []
    if errors:
        return False, errors, warnings

    xml = _read_content_xml(content)

    # 2. Required placeholders
    missing = [p for p in REQUIRED_PLACEHOLDERS[template_type] if p not in xml]
    if missing:
        errors.append('Es fehlen Platzhalter: ' + ', '.join(missing))

    # 3. Exactly one article row — _expand_item_rows clones the row it finds, so
    # a second one would silently end up as a stray line in every invoice.
    if template_type in ('rechnung', 'umbuchung'):
        rows = re.findall(
            r'<table:table-row\b[^>]*>(?:(?!</table:table-row>).)*?#ARTIKEL#.*?</table:table-row>',
            xml, re.DOTALL,
        )
        if len(rows) > 1:
            errors.append(
                f'Die Artikeltabelle enthält {len(rows)} Zeilen mit #ARTIKEL#. '
                'Es darf genau eine geben — sie wird pro Position automatisch vervielfältigt.'
            )

    # 4. Unknown placeholders — catches typos that would otherwise be printed verbatim
    known = set(REQUIRED_PLACEHOLDERS[template_type]) | OPTIONAL_PLACEHOLDERS
    found = set(re.findall(r'#[A-ZÄÖÜ][A-ZÄÖÜ0-9_ ]*#', xml))
    for unknown in sorted(found - known):
        warnings.append(
            f'Unbekannter Platzhalter {unknown} — er wird nicht ersetzt und '
            'erscheint so im fertigen PDF.'
        )

    if errors:
        return False, errors, warnings

    # 5. Probe render — the real safety net for everything the checks above miss
    ok, render_error = _probe_render(content, template_type)
    if not ok:
        errors.append(f'Testdruck fehlgeschlagen: {render_error}')

    return not errors, errors, warnings


def _probe_render(content: bytes, template_type: str):
    """Render the template once with dummy data. Returns (ok, error)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        template_path = os.path.join(temp_dir, 'probe.odt')
        with open(template_path, 'wb') as f:
            f.write(content)

        replacements = {p: 'Test' for p in REQUIRED_PLACEHOLDERS[template_type]}
        replacements['#HEUTE#'] = '01.01.2026'
        row_items = None
        signature_path = None

        if template_type == 'leihvertrag':
            replacements['#MATERIAL#'] = 'Testartikel 1\nTestartikel 2'
            signature_path = os.path.join(temp_dir, 'signature.png')
            with open(signature_path, 'wb') as f:
                f.write(_dummy_png())
        else:
            row_items = [{'name': 'Testartikel', 'quantity': 2, 'price': 1.5}]

        try:
            output_odt = os.path.join(temp_dir, 'probe_out.odt')
            process_odt_template(template_path, output_odt, replacements,
                                 signature_path, row_items=row_items)
            pdf_path = convert_to_pdf(output_odt, temp_dir)
        except Exception as e:
            return False, str(e)

        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            return False, 'Es wurde kein PDF erzeugt.'
        return True, None


def _dummy_png() -> bytes:
    """Minimal 1x1 PNG, used as a stand-in signature during the probe render."""
    import base64
    return base64.b64decode(
        b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
    )
