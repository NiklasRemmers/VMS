from flask import Blueprint, request, jsonify, render_template
from database import get_session
from models import InventoryItem, Bundle, BundleItem
from auth import login_required, current_user
from sqlalchemy.exc import IntegrityError

inventory_bp = Blueprint('inventory', __name__)

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
    data = request.get_json()
    name = data.get('name')
    description = data.get('description')

    if not name:
        return jsonify({'error': 'Name erforderlich'}), 400

    with get_session() as s:
        try:
            item = InventoryItem(name=name, description=description, type='equipment')
            s.add(item)
            s.commit()
            s.refresh(item)
            return jsonify(item.to_dict()), 201
        except IntegrityError:
            s.rollback()
            return jsonify({'error': 'Ein Gegenstand mit diesem Namen existiert bereits'}), 409
        except Exception as e:
            s.rollback()
            return jsonify({'error': str(e)}), 500

@inventory_bp.route('/api/inventory/items/<int:item_id>', methods=['PUT'])
@login_required
def update_item(item_id):
    data = request.get_json()
    
    with get_session() as s:
        item = s.query(InventoryItem).get(item_id)
        if not item:
            return jsonify({'error': 'Item nicht gefunden'}), 404
            
        if 'name' in data:
            item.name = data['name']
        if 'description' in data:
            item.description = data['description']
            
        try:
            s.commit()
            s.refresh(item)
            return jsonify(item.to_dict())
        except IntegrityError:
            s.rollback()
            return jsonify({'error': 'Name bereits vergeben'}), 409

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
    data = request.get_json()
    name = data.get('name')
    items = data.get('items', []) # List of {item_id: int, count: int}
    
    if not name:
        return jsonify({'error': 'Name erforderlich'}), 400

    with get_session() as s:
        try:
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
            return jsonify({'error': 'Ein Bundle mit diesem Namen existiert bereits'}), 409
        except Exception as e:
            return jsonify({'error': str(e)}), 500

@inventory_bp.route('/api/inventory/bundles/<int:bundle_id>', methods=['PUT'])
@login_required
def update_bundle(bundle_id):
    data = request.get_json()
    
    with get_session() as s:
        bundle = s.query(Bundle).get(bundle_id)
        if not bundle:
            return jsonify({'error': 'Bundle nicht gefunden'}), 404
            
        if 'name' in data:
            bundle.name = data['name']
            
        if 'items' in data:
            # Replace all items
            # Delete existing
            s.query(BundleItem).filter_by(bundle_id=bundle.id).delete()
            
            for item_data in data['items']:
                item_id = item_data.get('item_id')
                count = item_data.get('count', 1)
                if item_id:
                    s.add(BundleItem(bundle_id=bundle.id, item_id=item_id, count=count))
        
        try:
            s.commit()
            s.refresh(bundle)
            return jsonify(bundle.to_dict())
        except IntegrityError:
            return jsonify({'error': 'Name bereits vergeben'}), 409

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
