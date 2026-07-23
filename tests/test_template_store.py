"""Tests for template_store.py — versioned, KMS-encrypted ODT template store.

Fixture note: template_store.py calls ``kms.encrypt_binary``/``decrypt_binary``
without an explicit ``master_key``, so every call goes through
``kms.load_master_key()``. There is no SECRET_KEY fallback inside kms.py itself
(that fallback lives in security.py, a separate module) — so the autouse
``_kms_isolated`` fixture alone leaves KMS "unavailable" and any encrypt/decrypt
call here would raise FileNotFoundError. ``_kms_master_key`` below points
KMS_MASTER_KEY_PATH at a real key file for the duration of each test.

Second fixture note: template_store.py does
``from odt_processor import convert_to_pdf`` at *module import time*, so the
shared ``no_libreoffice`` fixture — which patches ``odt_processor.convert_to_pdf``
— never reaches this already-bound name (verified: patching the attribute on
the odt_processor module does not change a name already imported by value into
another module's namespace). ``stub_convert_to_pdf`` below patches the name
template_store actually calls, so no real LibreOffice subprocess ever runs.
"""
import hashlib
import io
import re
import zipfile

import pytest

import vms.infra.template_store as template_store
from vms.infra.template_store import ODT_MIMETYPE, REQUIRED_PLACEHOLDERS


MASTER_KEY = b"k" * 32


@pytest.fixture(autouse=True)
def _kms_master_key(tmp_path_factory, monkeypatch, _kms_isolated):
    import vms.infra.kms as kms

    key_path = tmp_path_factory.mktemp("kms") / "master.key"
    key_path.write_bytes(MASTER_KEY)
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(key_path))
    kms.clear_cache()
    yield


@pytest.fixture()
def stub_convert_to_pdf(mocker, tmp_path):
    fake_pdf = tmp_path / "probe.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    return mocker.patch("vms.infra.template_store.convert_to_pdf", return_value=str(fake_pdf))


# --------------------------------------------------------------------------
# Fixture helpers: build real & malformed ODT byte blobs
# --------------------------------------------------------------------------

def _bundled_bytes(template_type):
    with open(template_store.BUNDLED_TEMPLATES[template_type], "rb") as f:
        return f.read()


def _with_content_xml(original_bytes, transform):
    """Copy an ODT zip, passing content.xml's text through `transform`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original_bytes)) as zin, \
         zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "content.xml":
                data = transform(data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
    return buf.getvalue()


def _duplicate_artikel_row(xml):
    pattern = re.compile(
        r'<table:table-row\b[^>]*>(?:(?!</table:table-row>).)*?#ARTIKEL#.*?</table:table-row>',
        re.DOTALL,
    )
    m = pattern.search(xml)
    assert m, "fixture template has no #ARTIKEL# row"
    row = m.group(0)
    return xml[:m.end()] + row + xml[m.end():]


def _inject_unknown_placeholder(xml):
    assert "</office:text>" in xml
    return xml.replace("</office:text>", "<text:p>#FOO#</text:p></office:text>", 1)


def _minimal_odt(content_xml=b"<office:document-content/>", mimetype=ODT_MIMETYPE):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", mimetype)
        z.writestr("content.xml", content_xml)
    return buf.getvalue()


# ==========================================================================
# store_new_version
# ==========================================================================

@pytest.mark.integration
def test_store_new_version_on_fresh_type_becomes_version_2_after_seed(app, db_session):
    """ensure_seeded creates version 1 from the bundle first, so the first
    manual upload lands as version 2, not version 1."""
    from vms.domain.models import DocumentTemplate

    stored = template_store.store_new_version(
        "rechnung", "custom.odt", b"custom-bytes", user_id=None, note=None,
    )

    assert stored["version"] == 2
    assert stored["is_active"] is True
    db_session.expire_all()
    rows = db_session.query(DocumentTemplate).filter_by(template_type="rechnung").all()
    assert sorted(r.version for r in rows) == [1, 2]
    v1 = next(r for r in rows if r.version == 1)
    assert v1.is_active is False


@pytest.mark.integration
def test_store_new_version_increments_from_highest_existing_version(app, db_session):
    template_store.store_new_version("rechnung", "v2.odt", b"content-v2")
    stored3 = template_store.store_new_version("rechnung", "v3.odt", b"content-v3")

    assert stored3["version"] == 3


@pytest.mark.integration
def test_store_new_version_flips_prior_active_off_exactly_one_active(app, db_session):
    from vms.domain.models import DocumentTemplate

    template_store.store_new_version("rechnung", "v2.odt", b"content-v2")
    template_store.store_new_version("rechnung", "v3.odt", b"content-v3")

    db_session.expire_all()
    actives = (db_session.query(DocumentTemplate)
               .filter_by(template_type="rechnung", is_active=True).all())
    assert len(actives) == 1
    assert actives[0].version == 3


@pytest.mark.integration
def test_store_new_version_sets_content_hash_and_size_independently_computed(app, db_session):
    from vms.domain.models import DocumentTemplate

    content = b"\x00\x01hello-odt-bytes\xff"
    stored = template_store.store_new_version("rechnung", "f.odt", content)

    expected_hash = hashlib.sha256(content).hexdigest()
    db_session.expire_all()
    row = db_session.query(DocumentTemplate).filter_by(id=stored["id"]).one()
    assert row.content_hash == expected_hash
    assert row.size_bytes == len(content)


@pytest.mark.integration
def test_store_new_version_empty_note_is_stored_as_none(app, db_session):
    from vms.domain.models import DocumentTemplate

    stored = template_store.store_new_version("rechnung", "f.odt", b"x", note="")

    db_session.expire_all()
    row = db_session.query(DocumentTemplate).filter_by(id=stored["id"]).one()
    assert row.note is None


@pytest.mark.integration
def test_store_new_version_nonempty_note_is_preserved(app, db_session):
    from vms.domain.models import DocumentTemplate

    stored = template_store.store_new_version("rechnung", "f.odt", b"x", note="Layout-Fix")

    db_session.expire_all()
    row = db_session.query(DocumentTemplate).filter_by(id=stored["id"]).one()
    assert row.note == "Layout-Fix"


@pytest.mark.integration
def test_store_new_version_records_uploader(app, db_session, user):
    stored = template_store.store_new_version(
        "rechnung", "f.odt", b"x", user_id=user["id"],
    )

    from vms.domain.models import DocumentTemplate
    db_session.expire_all()
    row = db_session.query(DocumentTemplate).filter_by(id=stored["id"]).one()
    assert row.uploaded_by == user["id"]


# ==========================================================================
# get_content
# ==========================================================================

@pytest.mark.integration
def test_get_content_unknown_id_returns_none_none(app):
    filename, content = template_store.get_content(999999)

    assert (filename, content) == (None, None)


@pytest.mark.integration
def test_get_content_roundtrips_bytes_identically(app, db_session):
    payload = b"\x00\x01\xfe\xff some real bytes \x00"
    stored = template_store.store_new_version("rechnung", "roundtrip.odt", payload)

    filename, content = template_store.get_content(stored["id"])

    assert filename == "roundtrip.odt"
    assert content == payload
    assert content is not payload  # went through encrypt/decrypt, not identity passthrough


@pytest.mark.integration
def test_get_content_hash_matches_sha256_of_returned_bytes(app, db_session):
    from vms.domain.models import DocumentTemplate

    payload = b"content for hash check"
    stored = template_store.store_new_version("rechnung", "hash.odt", payload)

    _, content = template_store.get_content(stored["id"])

    db_session.expire_all()
    row = db_session.query(DocumentTemplate).filter_by(id=stored["id"]).one()
    assert row.content_hash == hashlib.sha256(content).hexdigest()


# ==========================================================================
# activate
# ==========================================================================

@pytest.mark.integration
def test_activate_unknown_id_returns_none(app):
    assert template_store.activate(999999) is None


@pytest.mark.integration
def test_activate_flips_others_off_and_makes_target_active(app, db_session):
    from vms.domain.models import DocumentTemplate

    v2 = template_store.store_new_version("rechnung", "v2.odt", b"v2")
    v3 = template_store.store_new_version("rechnung", "v3.odt", b"v3")
    assert v3["is_active"] is True  # v3 is currently active

    result = template_store.activate(v2["id"])

    assert result["id"] == v2["id"]
    assert result["is_active"] is True
    db_session.expire_all()
    actives = (db_session.query(DocumentTemplate)
               .filter_by(template_type="rechnung", is_active=True).all())
    assert [r.id for r in actives] == [v2["id"]]


# ==========================================================================
# delete_version
# ==========================================================================

@pytest.mark.integration
def test_delete_version_not_found(app):
    ok, error = template_store.delete_version(999999)

    assert (ok, error) == (False, "Version nicht gefunden")


@pytest.mark.integration
def test_delete_version_refuses_to_delete_the_active_version(app, db_session):
    from vms.domain.models import DocumentTemplate

    stored = template_store.store_new_version("rechnung", "active.odt", b"x")

    ok, error = template_store.delete_version(stored["id"])

    assert (ok, error) == (False, "Die aktive Version kann nicht gelöscht werden")
    db_session.expire_all()
    assert db_session.query(DocumentTemplate).filter_by(id=stored["id"]).first() is not None


@pytest.mark.integration
def test_delete_version_removes_an_inactive_version(app, db_session):
    from vms.domain.models import DocumentTemplate

    v2 = template_store.store_new_version("rechnung", "v2.odt", b"v2")
    template_store.store_new_version("rechnung", "v3.odt", b"v3")  # v3 now active, v2 inactive

    ok, error = template_store.delete_version(v2["id"])

    assert (ok, error) == (True, None)
    db_session.expire_all()
    assert db_session.query(DocumentTemplate).filter_by(id=v2["id"]).first() is None


# ==========================================================================
# ensure_seeded
# ==========================================================================

@pytest.mark.integration
def test_ensure_seeded_noop_for_non_bundled_type(app, db_session):
    from vms.domain.models import DocumentTemplate

    template_store.ensure_seeded("bogus")

    db_session.expire_all()
    assert db_session.query(DocumentTemplate).filter_by(template_type="bogus").count() == 0


@pytest.mark.integration
def test_ensure_seeded_creates_active_version_1_on_fresh_type(app, db_session):
    from vms.domain.models import DocumentTemplate

    template_store.ensure_seeded("umbuchung")

    db_session.expire_all()
    rows = db_session.query(DocumentTemplate).filter_by(template_type="umbuchung").all()
    assert len(rows) == 1
    assert rows[0].version == 1
    assert rows[0].is_active is True


@pytest.mark.integration
def test_ensure_seeded_is_idempotent(app, db_session):
    from vms.domain.models import DocumentTemplate

    template_store.ensure_seeded("umbuchung")
    template_store.ensure_seeded("umbuchung")

    db_session.expire_all()
    assert db_session.query(DocumentTemplate).filter_by(template_type="umbuchung").count() == 1


# ==========================================================================
# load_template
# ==========================================================================

@pytest.mark.integration
def test_load_template_unknown_type_raises_value_error(app, tmp_path):
    with pytest.raises(ValueError):
        template_store.load_template("bogus", str(tmp_path))


@pytest.mark.integration
def test_load_template_writes_bundled_file_and_returns_its_path(app, tmp_path):
    expected = _bundled_bytes("umbuchung")

    path = template_store.load_template("umbuchung", str(tmp_path))

    with open(path, "rb") as f:
        written = f.read()
    assert written == expected
    assert path.startswith(str(tmp_path))


@pytest.mark.integration
def test_load_template_uses_stored_versions_filename_after_upload(app, tmp_path, db_session):
    template_store.store_new_version("umbuchung", "mein_upload.odt", b"custom-content")

    path = template_store.load_template("umbuchung", str(tmp_path))

    import os
    assert os.path.basename(path) == "mein_upload.odt"
    with open(path, "rb") as f:
        assert f.read() == b"custom-content"


# ==========================================================================
# list_versions
# ==========================================================================

@pytest.mark.integration
def test_list_versions_seeds_and_returns_newest_first(app, db_session):
    versions = template_store.list_versions("umbuchung")

    assert [v["version"] for v in versions] == [1]
    assert versions[0]["is_active"] is True

    template_store.store_new_version("umbuchung", "v2.odt", b"v2")
    versions_after = template_store.list_versions("umbuchung")

    assert [v["version"] for v in versions_after] == [2, 1]  # newest first


# ==========================================================================
# _seed_from_bundle race re-check branch
# ==========================================================================

@pytest.mark.integration
def test_seed_from_bundle_returns_existing_active_without_second_insert(app, db_session):
    """Simulates the race: a version-1 row already exists when _seed_from_bundle
    runs (e.g. a concurrent request seeded it first). It must hit the
    `if existing:` re-check branch, not insert a duplicate version 1."""
    from vms.domain.models import DocumentTemplate
    from vms.infra.kms import encrypt_binary

    pre_existing_content = b"already-there"
    row = DocumentTemplate(
        template_type="leihvertrag",
        version=1,
        filename="preexisting.odt",
        encrypted_content=encrypt_binary(pre_existing_content),
        content_hash=hashlib.sha256(pre_existing_content).hexdigest(),
        size_bytes=len(pre_existing_content),
        is_active=True,
        note="pre-existing",
    )
    db_session.add(row)
    db_session.commit()

    result = template_store._seed_from_bundle("leihvertrag")

    assert result == pre_existing_content  # returns the existing active row's bytes, not the bundle's
    db_session.expire_all()
    assert db_session.query(DocumentTemplate).filter_by(template_type="leihvertrag").count() == 1


# ==========================================================================
# validate_template
# ==========================================================================

@pytest.mark.unit
def test_validate_template_unknown_type_is_rejected():
    ok, errors, warnings = template_store.validate_template(b"irrelevant", "bogus")

    assert ok is False
    assert "bogus" in errors[0]
    assert warnings == []


@pytest.mark.unit
def test_validate_template_not_a_zip_is_rejected_as_corrupt():
    ok, errors, warnings = template_store.validate_template(b"not a zip", "rechnung")

    assert ok is False
    assert "beschädigt" in errors[0]


@pytest.mark.unit
def test_validate_template_missing_content_xml_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", ODT_MIMETYPE)
    no_content_xml = buf.getvalue()

    ok, errors, warnings = template_store.validate_template(no_content_xml, "rechnung")

    assert ok is False
    assert any("content.xml" in e for e in errors)


@pytest.mark.unit
def test_validate_template_wrong_mimetype_is_rejected():
    wrong = _minimal_odt(mimetype=b"application/zip")

    ok, errors, warnings = template_store.validate_template(wrong, "rechnung")

    assert ok is False
    assert any("ODF-Textdokument" in e for e in errors)


@pytest.mark.unit
def test_validate_template_missing_required_placeholder_is_rejected():
    original = _bundled_bytes("rechnung")
    mutated = _with_content_xml(original, lambda xml: xml.replace("#HEUTE#", ""))

    ok, errors, warnings = template_store.validate_template(mutated, "rechnung")

    assert ok is False
    assert any("#HEUTE#" in e for e in errors)


@pytest.mark.unit
@pytest.mark.parametrize("template_type", ["rechnung", "umbuchung"])
def test_validate_template_rejects_more_than_one_artikel_row(template_type):
    original = _bundled_bytes(template_type)
    mutated = _with_content_xml(original, _duplicate_artikel_row)

    ok, errors, warnings = template_store.validate_template(mutated, template_type)

    assert ok is False
    assert any("2 Zeilen" in e for e in errors)


@pytest.mark.unit
def test_validate_template_unknown_placeholder_is_a_non_blocking_warning(stub_convert_to_pdf):
    original = _bundled_bytes("rechnung")
    mutated = _with_content_xml(original, _inject_unknown_placeholder)

    ok, errors, warnings = template_store.validate_template(mutated, "rechnung")

    assert errors == []
    assert any("#FOO#" in w for w in warnings)
    assert ok is True  # a warning alone must not block the upload


@pytest.mark.unit
def test_validate_template_valid_bundled_template_passes_with_no_findings(stub_convert_to_pdf):
    ok, errors, warnings = template_store.validate_template(
        _bundled_bytes("umbuchung"), "umbuchung",
    )

    assert (ok, errors, warnings) == (True, [], [])


@pytest.mark.unit
def test_validate_template_leihvertrag_has_no_artikel_row_check_and_probes_with_signature(
        stub_convert_to_pdf):
    """leihvertrag isn't in ('rechnung', 'umbuchung') -> the row-count branch is
    skipped, and the probe uses the signature-image code path instead of
    row_items."""
    ok, errors, warnings = template_store.validate_template(
        _bundled_bytes("leihvertrag"), "leihvertrag",
    )

    assert (ok, errors, warnings) == (True, [], [])


@pytest.mark.unit
def test_validate_template_probe_render_failure_is_appended_to_errors(mocker):
    mocker.patch("vms.infra.template_store.convert_to_pdf", return_value="/no/such/file.pdf")

    ok, errors, warnings = template_store.validate_template(
        _bundled_bytes("rechnung"), "rechnung",
    )

    assert ok is False
    assert any("Testdruck fehlgeschlagen" in e for e in errors)


@pytest.mark.unit
def test_validate_template_probe_render_exception_message_is_surfaced(mocker):
    mocker.patch("vms.infra.template_store.convert_to_pdf", side_effect=RuntimeError("libreoffice ist tot"))

    ok, errors, warnings = template_store.validate_template(
        _bundled_bytes("rechnung"), "rechnung",
    )

    assert ok is False
    assert any("libreoffice ist tot" in e for e in errors)
