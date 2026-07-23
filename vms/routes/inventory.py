from flask import Blueprint, request, jsonify, render_template, current_app
from vms.domain.database import get_session
from vms.domain.models import InventoryItem, Bundle, BundleItem
from vms.auth import login_required, current_user
from sqlalchemy.exc import IntegrityError, DataError

inventory_bp = Blueprint('inventory', __name__)


def _json_body():
    """JSON-Body als dict oder eine geordnete 400. Verhindert, dass ein
    nicht-JSON/`null`-Body zu None.get(...) -> AttributeError -> 500 wird."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({'error': 'Ungültiger oder fehlender JSON-Body'}), 400)
    return data, None


def _missing_item_ids(s, items):
    """Prüft die item_ids eines Bundle-Payloads vorab gegen die DB. Falsy ids
    werden (wie beim Anlegen) übersprungen. Gibt die nicht existierenden ids
    zurück -- so wird eine FK-Verletzung als präzise 400 gemeldet statt später
    fälschlich als Namenskonflikt (IntegrityError) durchzugehen."""
    item_ids = [it.get('item_id') for it in items if it.get('item_id')]
    if not item_ids:
        return []
    existing = {r[0] for r in s.query(InventoryItem.id)
                              .filter(InventoryItem.id.in_(item_ids)).all()}
    return [i for i in item_ids if i not in existing]

def _add_inventory_item(s, name, description, item_type='equipment', price=None, unit=None):
    """Lege einen Gegenstand an. Gemeinsamer Kern von create_item und der
    Alt-Route /api/materials/add, die das vorher eigenständig nachbaute.

    Wirft IntegrityError bei doppeltem Namen -- der Aufrufer entscheidet über
    Rollback und Antwortform.
    """
    item = InventoryItem(name=name, description=description, type=item_type)
    if item_type == 'consumable':
        if price is not None:
            item.price = price
        if unit:
            item.unit = unit
    s.add(item)
    s.commit()
    s.refresh(item)
    return item


@inventory_bp.route('/inventory')
@login_required
def inventory_page():
    return render_template('inventory.html', user=current_user)

@inventory_bp.route('/api/inventory/items', methods=['GET'])
@login_required
def get_items():
    with get_session() as s:
        items = s.query(InventoryItem).order_by(InventoryItem.name).all()
        return jsonify([i.to_dict() for i in items])

@inventory_bp.route('/api/inventory/items', methods=['POST'])
@login_required
def create_item():
    data, err = _json_body()
    if err:
        return err
    name = data.get('name')
    description = data.get('description')
    item_type = data.get('type', 'equipment')

    if not name:
        return jsonify({'error': 'Name erforderlich'}), 400
    if item_type not in ('equipment', 'consumable'):
        return jsonify({'error': 'Ungültiger Typ'}), 400

    with get_session() as s:
        try:
            item = _add_inventory_item(s, name, description, item_type,
                                       price=data.get('price'), unit=data.get('unit'))
            return jsonify(item.to_dict()), 201
        except IntegrityError:
            s.rollback()
            return jsonify({'error': 'Ein Gegenstand mit diesem Namen existiert bereits'}), 409
        except DataError:
            s.rollback()
            return jsonify({'error': 'Ungültige Eingabewerte'}), 400
        except Exception:  # pragma: no cover - defensiver Catch-all; nur über einen unerwarteten DB-Fehler erreichbar, der sich ohne Mock der Session nicht auslösen lässt
            s.rollback()
            current_app.logger.exception("Fehler beim Anlegen des Gegenstands")
            return jsonify({'error': 'Interner Fehler beim Speichern'}), 500

@inventory_bp.route('/api/inventory/items/<int:item_id>', methods=['PUT'])
@login_required
def update_item(item_id):
    data, err = _json_body()
    if err:
        return err

    with get_session() as s:
        item = s.query(InventoryItem).get(item_id)
        if not item:
            return jsonify({'error': 'Item nicht gefunden'}), 404

        if 'name' in data:
            item.name = data['name']
        if 'description' in data:
            item.description = data['description']
        if 'price' in data:
            item.price = data['price']
        if 'unit' in data:
            item.unit = data['unit']
            
        try:
            s.commit()
            s.refresh(item)
            return jsonify(item.to_dict())
        except IntegrityError:
            s.rollback()
            return jsonify({'error': 'Name bereits vergeben'}), 409
        except DataError:
            s.rollback()
            return jsonify({'error': 'Ungültige Eingabewerte'}), 400

@inventory_bp.route('/api/inventory/items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_item(item_id):
    with get_session() as s:
        item = s.query(InventoryItem).get(item_id)
        if not item:
            return jsonify({'error': 'Item nicht gefunden'}), 404
        
        s.delete(item)
        s.commit()
        return jsonify({'success': True})

# --- Bundles ---

@inventory_bp.route('/api/inventory/bundles', methods=['GET'])
@login_required
def get_bundles():
    with get_session() as s:
        bundles = s.query(Bundle).order_by(Bundle.name).all()
        return jsonify([b.to_dict() for b in bundles])

@inventory_bp.route('/api/inventory/bundles', methods=['POST'])
@login_required
def create_bundle():
    data, err = _json_body()
    if err:
        return err
    name = data.get('name')
    items = data.get('items', []) # List of {item_id: int, count: int}

    if not name:
        return jsonify({'error': 'Name erforderlich'}), 400

    with get_session() as s:
        try:
            missing = _missing_item_ids(s, items)
            if missing:
                return jsonify({'error': f'Unbekannte Item-ID(s): {missing}'}), 400

            bundle = Bundle(name=name)
            s.add(bundle)
            s.flush() # Get ID

            for item_data in items:
                item_id = item_data.get('item_id')
                count = item_data.get('count', 1)
                if item_id:
                    s.add(BundleItem(bundle_id=bundle.id, item_id=item_id, count=count))

            s.commit()
            # Refresh to load relationships
            s.refresh(bundle)
            return jsonify(bundle.to_dict()), 201
        except IntegrityError:
            # Rollback vor dem Return: sonst committet get_session() beim
            # normalen Blockende eine abgebrochene Transaktion und diese 409
            # wird zur unbehandelten 500.
            s.rollback()
            return jsonify({'error': 'Ein Bundle mit diesem Namen existiert bereits'}), 409
        except DataError:
            s.rollback()
            return jsonify({'error': 'Ungültige Eingabewerte'}), 400
        except Exception:  # pragma: no cover - defensiver Catch-all; nur über einen unerwarteten DB-Fehler erreichbar, der sich ohne Mock der Session nicht auslösen lässt
            s.rollback()
            current_app.logger.exception("Fehler beim Anlegen des Bundles")
            return jsonify({'error': 'Interner Fehler beim Speichern'}), 500

@inventory_bp.route('/api/inventory/bundles/<int:bundle_id>', methods=['PUT'])
@login_required
def update_bundle(bundle_id):
    data, err = _json_body()
    if err:
        return err

    with get_session() as s:
        bundle = s.query(Bundle).get(bundle_id)
        if not bundle:
            return jsonify({'error': 'Bundle nicht gefunden'}), 404

        try:
            if 'name' in data:
                bundle.name = data['name']

            if 'items' in data:
                missing = _missing_item_ids(s, data['items'])
                if missing:
                    return jsonify({'error': f'Unbekannte Item-ID(s): {missing}'}), 400

                # Replace all items
                # Delete existing
                s.query(BundleItem).filter_by(bundle_id=bundle.id).delete()

                for item_data in data['items']:
                    item_id = item_data.get('item_id')
                    count = item_data.get('count', 1)
                    if item_id:
                        s.add(BundleItem(bundle_id=bundle.id, item_id=item_id, count=count))

            s.commit()
            s.refresh(bundle)
            return jsonify(bundle.to_dict())
        except IntegrityError:
            # Siehe create_bundle: ohne Rollback wird aus dieser 409 eine 500.
            s.rollback()
            return jsonify({'error': 'Name bereits vergeben'}), 409
        except DataError:
            s.rollback()
            return jsonify({'error': 'Ungültige Eingabewerte'}), 400

@inventory_bp.route('/api/inventory/bundles/<int:bundle_id>', methods=['DELETE'])
@login_required
def delete_bundle(bundle_id):
    with get_session() as s:
        bundle = s.query(Bundle).get(bundle_id)
        if not bundle:
            return jsonify({'error': 'Bundle nicht gefunden'}), 404
        
        s.delete(bundle)
        s.commit()
        return jsonify({'success': True})


# Aus app.py hierher gezogen: die Materialrouten gehören fachlich zum
# Inventar. '/api/materials/add' legte dabei InventoryItem direkt an und
# duplizierte create_item -- es delegiert jetzt an dieselbe Logik.
@inventory_bp.route('/api/materials', methods=['GET'])
@login_required
def get_materials():
    """Return the available materials from DB (Items) and Bundles as packages."""
    from vms.domain.database import get_session
    from vms.domain.models import InventoryItem, Bundle, BundleItem
    
    try:
        with get_session() as s:
            items = s.query(InventoryItem).order_by(InventoryItem.name).all()
            bundles = s.query(Bundle).order_by(Bundle.name).all()
            
            # Format: { "materials": { "Name": "Description" } } (Backwards compatibility)
            materials_dict = {}
            equipment_dict = {}
            
            for item in items:
                if item.type == 'case':
                    continue  # Skip legacy case items
                desc = item.description or item.name
                materials_dict[item.name] = desc
                equipment_dict[item.name] = desc
            
            # Format: { "packages": { "BundleName": [ { "count": 1, "text": "Description" } ] } }
            packages_dict = {}
            for bundle in bundles:
                package_items = []
                for b_item in bundle.items:
                    item_def = s.query(InventoryItem).get(b_item.item_id)
                    if item_def:
                        package_items.append({
                            'count': b_item.count,
                            'text': item_def.name
                        })
                packages_dict[bundle.name] = package_items
                
            return jsonify({
                "materials": materials_dict,
                "equipment": equipment_dict,
                "packages": packages_dict
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@inventory_bp.route('/api/materials/add', methods=['POST'])
@login_required
def add_material():
    """Alt-Route des Vertragsformulars. Antwortform ({'success', 'name', 'text'})
    ist bewusst unverändert, das Frontend hängt daran; die Anlage selbst läuft
    jetzt über denselben Kern wie create_item."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    text = (data.get('text') or '').strip()

    if not name or not text:
        return jsonify({'error': 'Name und Beschreibung erforderlich'}), 400

    with get_session() as s:
        try:
            _add_inventory_item(s, name, text, 'equipment')
            return jsonify({'success': True, 'name': name, 'text': text})
        except IntegrityError:
            s.rollback()
            return jsonify({'error': 'Existiert bereits'}), 409
        except DataError:
            s.rollback()
            return jsonify({'error': 'Ungültige Eingabewerte'}), 400
        except Exception:  # pragma: no cover - defensiver Catch-all; nur über einen unerwarteten DB-Fehler erreichbar, der sich ohne Mock der Session nicht auslösen lässt
            s.rollback()
            current_app.logger.exception("Fehler beim Anlegen des Materials")
            return jsonify({'error': 'Interner Fehler beim Speichern'}), 500
