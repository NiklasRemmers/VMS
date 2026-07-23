"""Tests für inventory_routes.py.

Schwerpunkt hier: die Transaktionsgrenze. `database.get_session()` committet bei
normalem Blockende. Wird ein IntegrityError *innerhalb* des with-Blocks gefangen
und dort returnt, verlässt der Block normal -- der Kontextmanager committet dann
eine Session, deren Transaktion abgebrochen ist. Unter SQLAlchemy 2.x wirft das
PendingRollbackError, und die eigentlich gemeinte 409 kommt nie beim Client an.
"""
import pytest


SQLALCHEMY_INTERNALS = ("PendingRollbackError", "rollback", "sqlalchemy",
                        "IntegrityError", "psycopg2", "DETAIL:")


def _make_item(db_session, name="Zelt", **overrides):
    from vms.domain.models import InventoryItem
    defaults = dict(name=name, description="Ein Zelt", type="equipment")
    defaults.update(overrides)
    item = InventoryItem(**defaults)
    db_session.add(item)
    db_session.flush()
    return item.id


def _make_bundle(db_session, name="Grundausstattung"):
    from vms.domain.models import Bundle
    b = Bundle(name=name)
    db_session.add(b)
    db_session.flush()
    return b.id


def _assert_no_internals(error_message):
    """Die Fehlermeldung an den Client darf keine SQLAlchemy-Interna tragen."""
    low = (error_message or "").lower()
    leaked = [t for t in SQLALCHEMY_INTERNALS if t.lower() in low]
    assert not leaked, f"Interner Fehlertext an den Client durchgereicht: {leaked} in {error_message!r}"


# --------------------------------------------------------------------------
# Bundles: doppelter Name muss 409 liefern, nicht 500
# --------------------------------------------------------------------------

@pytest.mark.route
def test_create_bundle_with_duplicate_name_returns_409(auth_client, user, db_session):
    _make_bundle(db_session, name="Grundausstattung")
    db_session.commit()

    resp = auth_client.post("/api/inventory/bundles",
                            json={"name": "Grundausstattung", "items": []})

    assert resp.status_code == 409
    _assert_no_internals(resp.get_json().get("error"))


@pytest.mark.route
def test_create_bundle_duplicate_does_not_persist_a_second_row(auth_client, user, db_session):
    from vms.domain.models import Bundle

    _make_bundle(db_session, name="Grundausstattung")
    db_session.commit()

    auth_client.post("/api/inventory/bundles",
                     json={"name": "Grundausstattung", "items": []})

    db_session.expire_all()
    assert db_session.query(Bundle).filter_by(name="Grundausstattung").count() == 1


@pytest.mark.route
def test_update_bundle_to_an_existing_name_returns_409(auth_client, user, db_session):
    from vms.domain.models import Bundle

    _make_bundle(db_session, name="Grundausstattung")
    other = _make_bundle(db_session, name="Zusatzpaket")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{other}",
                           json={"name": "Grundausstattung"})

    assert resp.status_code == 409
    _assert_no_internals(resp.get_json().get("error"))
    # Der Umbenennungsversuch darf nicht teilweise durchgeschlagen sein.
    db_session.expire_all()
    assert db_session.query(Bundle).filter_by(id=other).one().name == "Zusatzpaket"


@pytest.mark.route
def test_create_bundle_happy_path_persists_items(auth_client, user, db_session):
    from vms.domain.models import Bundle, BundleItem

    item_id = _make_item(db_session, name="Bierbank")
    db_session.commit()

    resp = auth_client.post("/api/inventory/bundles", json={
        "name": "Festpaket",
        "items": [{"item_id": item_id, "count": 4}],
    })

    assert resp.status_code == 201
    db_session.expire_all()
    bundle = db_session.query(Bundle).filter_by(name="Festpaket").one()
    links = db_session.query(BundleItem).filter_by(bundle_id=bundle.id).all()
    assert [(l.item_id, l.count) for l in links] == [(item_id, 4)]


@pytest.mark.route
def test_create_bundle_without_name_returns_400(auth_client, user, db_session):
    resp = auth_client.post("/api/inventory/bundles", json={"items": []})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Name erforderlich"


@pytest.mark.route
def test_update_unknown_bundle_returns_404(auth_client, user, db_session):
    resp = auth_client.put("/api/inventory/bundles/999999", json={"name": "Egal"})

    assert resp.status_code == 404


@pytest.mark.route
def test_bundles_endpoint_anonymous_is_unauthorized(client):
    resp = client.get("/api/inventory/bundles", follow_redirects=False)
    assert resp.status_code in (302, 401)


# --------------------------------------------------------------------------
# /api/materials/add: Alt-Route des Vertragsformulars, in Block B aus app.py
# hierher gezogen und mit create_item auf einen gemeinsamen Kern gelegt.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_add_material_with_duplicate_name_returns_409(auth_client, user, db_session):
    _make_item(db_session, name="Zelt")
    db_session.commit()

    resp = auth_client.post("/api/materials/add",
                            json={"name": "Zelt", "text": "Noch ein Zelt"})

    assert resp.status_code == 409
    assert resp.get_json()["error"] == "Existiert bereits"


@pytest.mark.route
def test_add_material_duplicate_does_not_leak_sqlalchemy_internals(auth_client, user, db_session):
    _make_item(db_session, name="Zelt")
    db_session.commit()

    resp = auth_client.post("/api/materials/add",
                            json={"name": "Zelt", "text": "Noch ein Zelt"})

    _assert_no_internals(resp.get_json().get("error"))


@pytest.mark.route
def test_add_material_duplicate_does_not_persist_a_second_row(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    _make_item(db_session, name="Zelt")
    db_session.commit()

    auth_client.post("/api/materials/add", json={"name": "Zelt", "text": "Noch eins"})

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter_by(name="Zelt").count() == 1


@pytest.mark.route
def test_add_material_happy_path_persists(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    resp = auth_client.post("/api/materials/add",
                            json={"name": "Pavillon", "text": "3x3m"})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(name="Pavillon").one()
    assert row.description == "3x3m"
    assert row.type == "equipment"


@pytest.mark.route
@pytest.mark.parametrize("payload", [
    {"name": "", "text": "Beschreibung"},
    {"name": "Zelt", "text": ""},
    {},
])
def test_add_material_rejects_incomplete_payload(auth_client, user, db_session, payload):
    resp = auth_client.post("/api/materials/add", json=payload)

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Name und Beschreibung erforderlich"


@pytest.mark.route
def test_add_material_with_oversized_name_returns_clean_400_no_leak(auth_client, user, db_session):
    resp = auth_client.post("/api/materials/add", json={
        "name": "x" * 300, "text": "Beschreibung",
    })

    assert resp.status_code == 400
    _assert_no_internals(resp.get_json().get("error"))


@pytest.mark.route
def test_add_material_anonymous_is_unauthorized(client):
    resp = client.post("/api/materials/add", json={"name": "X", "text": "Y"},
                       follow_redirects=False)
    assert resp.status_code in (302, 401)


# --------------------------------------------------------------------------
# inventory_page (GET /inventory) -- template rendering + auth boundary
# --------------------------------------------------------------------------

@pytest.mark.route
def test_inventory_page_authenticated_renders(auth_client, user, db_session):
    resp = auth_client.get("/inventory")

    assert resp.status_code == 200
    assert b"inventory" in resp.data.lower() or b"inventar" in resp.data.lower()


@pytest.mark.route
def test_inventory_page_anonymous_is_unauthorized(client):
    resp = client.get("/inventory", follow_redirects=False)
    assert resp.status_code in (302, 401)


# --------------------------------------------------------------------------
# create_item (POST /api/inventory/items)
# --------------------------------------------------------------------------

@pytest.mark.route
def test_create_item_happy_path_equipment_default(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    resp = auth_client.post("/api/inventory/items", json={
        "name": "Zelt",
        "description": "Ein grosses Zelt",
    })

    assert resp.status_code == 201
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(name="Zelt").one()
    assert row.description == "Ein grosses Zelt"
    assert row.type == "equipment"


@pytest.mark.route
def test_create_item_consumable_persists_price_and_unit(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    resp = auth_client.post("/api/inventory/items", json={
        "name": "Kohle",
        "description": "Grillkohle",
        "type": "consumable",
        "price": 12.5,
        "unit": "kg",
    })

    assert resp.status_code == 201
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(name="Kohle").one()
    assert row.type == "consumable"
    assert float(row.price) == 12.5
    assert row.unit == "kg"


@pytest.mark.route
def test_create_item_consumable_without_price_or_unit_leaves_them_none(auth_client, user, db_session):
    """Exercises the false branch of both `if price is not None` and `if unit`
    inside _add_inventory_item -- a consumable can be created before its price
    list is known."""
    from vms.domain.models import InventoryItem

    resp = auth_client.post("/api/inventory/items", json={
        "name": "Sirup",
        "description": "Ohne Preis",
        "type": "consumable",
    })

    assert resp.status_code == 201
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(name="Sirup").one()
    assert row.type == "consumable"
    assert row.price is None
    assert row.unit is None


@pytest.mark.route
def test_create_item_equipment_ignores_price_and_unit(auth_client, user, db_session):
    """price/unit are only applied for type == 'consumable' (_add_inventory_item)."""
    from vms.domain.models import InventoryItem

    resp = auth_client.post("/api/inventory/items", json={
        "name": "Fahne",
        "description": "Vereinsfahne",
        "type": "equipment",
        "price": 99,
        "unit": "Stück",
    })

    assert resp.status_code == 201
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(name="Fahne").one()
    assert row.price is None
    assert row.unit is None


@pytest.mark.route
def test_create_item_without_name_returns_400(auth_client, user, db_session):
    resp = auth_client.post("/api/inventory/items", json={"description": "Kein Name"})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Name erforderlich"


@pytest.mark.route
def test_create_item_invalid_type_returns_400(auth_client, user, db_session):
    resp = auth_client.post("/api/inventory/items", json={
        "name": "Ding", "type": "gadget",
    })

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Ungültiger Typ"


@pytest.mark.route
def test_create_item_duplicate_name_returns_409_and_no_second_row(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    _make_item(db_session, name="Zelt")
    db_session.commit()

    resp = auth_client.post("/api/inventory/items", json={"name": "Zelt", "description": "Noch eins"})

    assert resp.status_code == 409
    _assert_no_internals(resp.get_json().get("error"))
    db_session.expire_all()
    assert db_session.query(InventoryItem).filter_by(name="Zelt").count() == 1


@pytest.mark.route
def test_create_item_invalid_price_value_returns_clean_400_no_leak(auth_client, user, db_session):
    resp = auth_client.post("/api/inventory/items", json={
        "name": "Probe", "type": "consumable", "price": "not-a-number",
    })

    assert resp.status_code == 400
    _assert_no_internals(resp.get_json().get("error"))


@pytest.mark.route
def test_create_item_anonymous_is_unauthorized(client):
    resp = client.post("/api/inventory/items", json={"name": "X"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


@pytest.mark.route
def test_create_item_with_non_json_body_returns_400_not_500(auth_client, user, db_session):
    resp = auth_client.post("/api/inventory/items", data="not json",
                            content_type="text/plain")

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# update_item (PUT /api/inventory/items/<id>) -- the four independent
# key-presence branches, plus not-found and duplicate-name rollback.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_update_item_name_only_updates_just_name(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    item_id = _make_item(db_session, name="Alt", description="Beschreibung", type="consumable",
                         price=5, unit="Stück")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/items/{item_id}", json={"name": "Neu"})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(id=item_id).one()
    assert row.name == "Neu"
    assert row.description == "Beschreibung"
    assert float(row.price) == 5
    assert row.unit == "Stück"


@pytest.mark.route
def test_update_item_description_only_updates_just_description(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    item_id = _make_item(db_session, name="Zelt", description="Alt")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/items/{item_id}", json={"description": "Neu"})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(id=item_id).one()
    assert row.name == "Zelt"
    assert row.description == "Neu"


@pytest.mark.route
def test_update_item_price_only_updates_just_price(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    item_id = _make_item(db_session, name="Kohle", type="consumable", price=1, unit="kg")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/items/{item_id}", json={"price": 7.5})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(id=item_id).one()
    assert float(row.price) == 7.5
    assert row.unit == "kg"


@pytest.mark.route
def test_update_item_unit_only_updates_just_unit(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    item_id = _make_item(db_session, name="Kohle", type="consumable", price=1, unit="kg")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/items/{item_id}", json={"unit": "Liter"})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(InventoryItem).filter_by(id=item_id).one()
    assert row.unit == "Liter"
    assert float(row.price) == 1


@pytest.mark.route
def test_update_item_unknown_id_returns_404(auth_client, user, db_session):
    resp = auth_client.put("/api/inventory/items/999999", json={"name": "Egal"})

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Item nicht gefunden"


@pytest.mark.route
def test_update_item_duplicate_name_returns_409_and_rolls_back(auth_client, user, db_session):
    from vms.domain.models import InventoryItem

    _make_item(db_session, name="Zelt")
    other_id = _make_item(db_session, name="Pavillon")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/items/{other_id}", json={"name": "Zelt"})

    assert resp.status_code == 409
    _assert_no_internals(resp.get_json().get("error"))
    db_session.expire_all()
    assert db_session.query(InventoryItem).filter_by(id=other_id).one().name == "Pavillon"


@pytest.mark.route
def test_update_item_with_non_json_body_returns_400_not_500(auth_client, user, db_session):
    item_id = _make_item(db_session, name="Zelt")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/items/{item_id}", data="not json",
                           content_type="text/plain")

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# delete_item / delete_bundle: not-found + cascade of BundleItem rows
# --------------------------------------------------------------------------

@pytest.mark.route
def test_delete_item_unknown_id_returns_404(auth_client, user, db_session):
    resp = auth_client.delete("/api/inventory/items/999999")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Item nicht gefunden"


@pytest.mark.route
def test_delete_item_removes_row_and_cascades_bundle_items(auth_client, user, db_session):
    from vms.domain.models import InventoryItem, Bundle, BundleItem

    item_id = _make_item(db_session, name="Zelt")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.add(BundleItem(bundle_id=bundle_id, item_id=item_id, count=2))
    db_session.commit()

    resp = auth_client.delete(f"/api/inventory/items/{item_id}")

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    db_session.expire_all()
    assert db_session.query(InventoryItem).filter_by(id=item_id).count() == 0
    assert db_session.query(BundleItem).filter_by(item_id=item_id).count() == 0
    # The bundle itself must survive -- only the link row is gone.
    assert db_session.query(Bundle).filter_by(id=bundle_id).count() == 1


@pytest.mark.route
def test_delete_bundle_unknown_id_returns_404(auth_client, user, db_session):
    resp = auth_client.delete("/api/inventory/bundles/999999")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Bundle nicht gefunden"


@pytest.mark.route
def test_delete_bundle_removes_row_and_cascades_bundle_items(auth_client, user, db_session):
    from vms.domain.models import Bundle, BundleItem

    item_id = _make_item(db_session, name="Zelt")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.add(BundleItem(bundle_id=bundle_id, item_id=item_id, count=2))
    db_session.commit()

    resp = auth_client.delete(f"/api/inventory/bundles/{bundle_id}")

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    db_session.expire_all()
    assert db_session.query(Bundle).filter_by(id=bundle_id).count() == 0
    assert db_session.query(BundleItem).filter_by(bundle_id=bundle_id).count() == 0


@pytest.mark.route
def test_delete_item_anonymous_is_unauthorized(client):
    resp = client.delete("/api/inventory/items/1", follow_redirects=False)
    assert resp.status_code in (302, 401)


@pytest.mark.route
def test_delete_bundle_anonymous_is_unauthorized(client):
    resp = client.delete("/api/inventory/bundles/1", follow_redirects=False)
    assert resp.status_code in (302, 401)


@pytest.mark.route
def test_create_bundle_invalid_count_value_returns_clean_400_no_leak(auth_client, user, db_session):
    item_id = _make_item(db_session, name="Zelt")
    db_session.commit()

    resp = auth_client.post("/api/inventory/bundles", json={
        "name": "Probebundle",
        "items": [{"item_id": item_id, "count": "not-an-int"}],
    })

    assert resp.status_code == 400
    _assert_no_internals(resp.get_json().get("error"))


# --------------------------------------------------------------------------
# update_bundle: items-replacement is independent of the name-update branch
# --------------------------------------------------------------------------

@pytest.mark.route
def test_update_bundle_items_replaces_all_links(auth_client, user, db_session):
    from vms.domain.models import BundleItem

    item_a = _make_item(db_session, name="Zelt")
    item_b = _make_item(db_session, name="Pavillon")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.add(BundleItem(bundle_id=bundle_id, item_id=item_a, count=1))
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={
        "items": [{"item_id": item_b, "count": 3}],
    })

    assert resp.status_code == 200
    db_session.expire_all()
    links = db_session.query(BundleItem).filter_by(bundle_id=bundle_id).all()
    assert [(l.item_id, l.count) for l in links] == [(item_b, 3)]


@pytest.mark.route
def test_update_bundle_name_only_leaves_items_untouched(auth_client, user, db_session):
    from vms.domain.models import Bundle, BundleItem

    item_a = _make_item(db_session, name="Zelt")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.add(BundleItem(bundle_id=bundle_id, item_id=item_a, count=5))
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={"name": "Umbenannt"})

    assert resp.status_code == 200
    db_session.expire_all()
    bundle = db_session.query(Bundle).filter_by(id=bundle_id).one()
    assert bundle.name == "Umbenannt"
    links = db_session.query(BundleItem).filter_by(bundle_id=bundle_id).all()
    assert [(l.item_id, l.count) for l in links] == [(item_a, 5)]


@pytest.mark.route
def test_update_bundle_name_and_items_both_applied(auth_client, user, db_session):
    from vms.domain.models import Bundle, BundleItem

    item_a = _make_item(db_session, name="Zelt")
    item_b = _make_item(db_session, name="Pavillon")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.add(BundleItem(bundle_id=bundle_id, item_id=item_a, count=1))
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={
        "name": "Neupaket",
        "items": [{"item_id": item_b, "count": 2}],
    })

    assert resp.status_code == 200
    db_session.expire_all()
    bundle = db_session.query(Bundle).filter_by(id=bundle_id).one()
    assert bundle.name == "Neupaket"
    links = db_session.query(BundleItem).filter_by(bundle_id=bundle_id).all()
    assert [(l.item_id, l.count) for l in links] == [(item_b, 2)]


@pytest.mark.route
def test_update_bundle_neither_name_nor_items_leaves_bundle_unchanged(auth_client, user, db_session):
    from vms.domain.models import Bundle, BundleItem

    item_a = _make_item(db_session, name="Zelt")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.add(BundleItem(bundle_id=bundle_id, item_id=item_a, count=1))
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={})

    assert resp.status_code == 200
    db_session.expire_all()
    bundle = db_session.query(Bundle).filter_by(id=bundle_id).one()
    assert bundle.name == "Festpaket"
    links = db_session.query(BundleItem).filter_by(bundle_id=bundle_id).all()
    assert [(l.item_id, l.count) for l in links] == [(item_a, 1)]


@pytest.mark.route
def test_update_bundle_with_nonexistent_item_id_returns_clean_4xx_no_leak(auth_client, user, db_session):
    """The item_ids are validated against the DB before insert, so an invalid
    item_id is rejected with a precise 400 (no FK IntegrityError, no internals
    leaked) instead of the misleading duplicate-name 409 it used to produce."""
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={
        "items": [{"item_id": 999999, "count": 1}],
    })

    assert resp.status_code == 400
    _assert_no_internals(resp.get_json().get("error"))


@pytest.mark.route
def test_update_bundle_with_nonexistent_item_id_error_message_mentions_item(auth_client, user, db_session):
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={
        "items": [{"item_id": 999999, "count": 1}],
    })

    error = resp.get_json().get("error", "")
    assert "name" not in error.lower()


@pytest.mark.route
def test_update_bundle_items_entry_with_falsy_item_id_is_silently_skipped(auth_client, user, db_session):
    """Documents current behaviour: `if item_id:` treats item_id 0/None/missing
    as absent, so such an entry vanishes from the bundle without any error."""
    from vms.domain.models import BundleItem

    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={
        "items": [{"item_id": 0, "count": 1}, {"count": 2}],
    })

    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(BundleItem).filter_by(bundle_id=bundle_id).count() == 0


@pytest.mark.route
def test_update_bundle_anonymous_is_unauthorized(client):
    resp = client.put("/api/inventory/bundles/1", json={"name": "X"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


# --------------------------------------------------------------------------
# create_bundle: same falsy item_id branch, arranged directly at create time
# --------------------------------------------------------------------------

@pytest.mark.route
def test_create_bundle_items_entry_with_falsy_item_id_is_silently_skipped(auth_client, user, db_session):
    from vms.domain.models import BundleItem, Bundle

    resp = auth_client.post("/api/inventory/bundles", json={
        "name": "Leerpaket",
        "items": [{"item_id": 0, "count": 1}, {"count": 2}],
    })

    assert resp.status_code == 201
    db_session.expire_all()
    bundle = db_session.query(Bundle).filter_by(name="Leerpaket").one()
    assert db_session.query(BundleItem).filter_by(bundle_id=bundle.id).count() == 0


# --------------------------------------------------------------------------
# Read routes: get_items / get_bundles ordering, get_materials happy path
# and the 'case'-type skip branch, add_material's legacy 200 status.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_get_items_returns_ordered_by_name(auth_client, user, db_session):
    _make_item(db_session, name="Zelt")
    _make_item(db_session, name="Anhänger")
    _make_item(db_session, name="Musik")
    db_session.commit()

    resp = auth_client.get("/api/inventory/items")

    assert resp.status_code == 200
    names = [i["name"] for i in resp.get_json()]
    assert names == ["Anhänger", "Musik", "Zelt"]


@pytest.mark.route
def test_get_bundles_returns_ordered_by_name(auth_client, user, db_session):
    _make_bundle(db_session, name="Zusatzpaket")
    _make_bundle(db_session, name="Grundausstattung")
    db_session.commit()

    resp = auth_client.get("/api/inventory/bundles")

    assert resp.status_code == 200
    names = [b["name"] for b in resp.get_json()]
    assert names == ["Grundausstattung", "Zusatzpaket"]


@pytest.mark.route
def test_get_materials_happy_path_shape(auth_client, user, db_session):
    from vms.domain.models import BundleItem

    item_id = _make_item(db_session, name="Zelt", description="Ein Zelt")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.add(BundleItem(bundle_id=bundle_id, item_id=item_id, count=2))
    db_session.commit()

    resp = auth_client.get("/api/materials")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["materials"]["Zelt"] == "Ein Zelt"
    assert body["equipment"]["Zelt"] == "Ein Zelt"
    assert body["packages"]["Festpaket"] == [{"count": 2, "text": "Zelt"}]


@pytest.mark.route
def test_get_materials_skips_case_type_items(auth_client, user, db_session):
    _make_item(db_session, name="Sichtschutzwand", type="case")
    _make_item(db_session, name="Zelt", type="equipment")
    db_session.commit()

    resp = auth_client.get("/api/materials")

    assert resp.status_code == 200
    body = resp.get_json()
    assert "Sichtschutzwand" not in body["materials"]
    assert "Sichtschutzwand" not in body["equipment"]
    assert "Zelt" in body["materials"]


@pytest.mark.route
def test_get_materials_anonymous_is_unauthorized(client):
    resp = client.get("/api/materials", follow_redirects=False)
    assert resp.status_code in (302, 401)


@pytest.mark.route
def test_add_material_returns_200_not_201_on_create(auth_client, user, db_session):
    """Pinning the legacy contract: unlike create_item (201), this route
    always answers 200, even though it creates a row."""
    resp = auth_client.post("/api/materials/add", json={"name": "Kiste", "text": "Holzkiste"})

    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Findings-Fixes: die entschärften Fehlerzweige (DataError -> saubere 400,
# nicht-JSON Body -> 400, ungültige item_id -> präzise 400) auf den Bundle-
# und Update-Routen, die ihre Regressionstests noch nicht hatten.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_update_item_invalid_price_value_returns_clean_400(auth_client, user, db_session):
    item_id = _make_item(db_session, name="Zelt", type="consumable")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/items/{item_id}", json={"price": "not-a-number"})

    assert resp.status_code == 400
    _assert_no_internals(resp.get_json().get("error"))


@pytest.mark.route
def test_create_bundle_with_non_json_body_returns_400(auth_client, user, db_session):
    resp = auth_client.post("/api/inventory/bundles", data="not json",
                            content_type="text/plain")

    assert resp.status_code == 400


@pytest.mark.route
def test_update_bundle_with_non_json_body_returns_400(auth_client, user, db_session):
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", data="not json",
                           content_type="text/plain")

    assert resp.status_code == 400


@pytest.mark.route
def test_create_bundle_with_nonexistent_item_id_returns_400(auth_client, user, db_session):
    resp = auth_client.post("/api/inventory/bundles", json={
        "name": "Probebundle",
        "items": [{"item_id": 999999, "count": 1}],
    })

    assert resp.status_code == 400
    error = resp.get_json().get("error", "")
    assert "item" in error.lower()
    _assert_no_internals(error)


@pytest.mark.route
def test_update_bundle_invalid_count_value_returns_clean_400(auth_client, user, db_session):
    item_id = _make_item(db_session, name="Zelt")
    bundle_id = _make_bundle(db_session, name="Festpaket")
    db_session.commit()

    resp = auth_client.put(f"/api/inventory/bundles/{bundle_id}", json={
        "items": [{"item_id": item_id, "count": "not-an-int"}],
    })

    assert resp.status_code == 400
    _assert_no_internals(resp.get_json().get("error"))
