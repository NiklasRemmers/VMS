"""
Kanboard API Client
Handles communication with the Kanboard TODO board
Supports per-user configuration.
"""
import requests
from typing import Dict, List, Optional, Any
from database import get_user_settings
from security import decrypt_value

DEFAULT_PROJECT_ID = 25  # Fallback

def get_project_id(user_id: int) -> int:
    """Get project ID from user settings or default."""
    settings = get_user_settings(user_id)
    if not settings:
        return DEFAULT_PROJECT_ID
    return settings.get('kanboard_project_id') or DEFAULT_PROJECT_ID

def _make_request(user_id: int, method: str, params: Dict = None) -> Any:
    """Make a JSON-RPC request to Kanboard API for specific user."""
    settings = get_user_settings(user_id)
    if not settings:
        raise ValueError("Keine Kanboard-Einstellungen für Benutzer gefunden.")
        
    url = settings.get('kanboard_url')
    username = settings.get('kanboard_user')
    token = decrypt_value(settings.get('encrypted_kanboard_token'))
    
    if not url or not username or not token:
        raise ValueError("Kanboard-Konfiguration unvollständig.")
        
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": 1,
        "params": params or {}
    }
    
    try:
        response = requests.post(
            url,
            json=payload,
            auth=(username, token),
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        response.raise_for_status()
        result = response.json()
        
        if 'error' in result:
            raise Exception(result['error'].get('message', 'Unknown API error'))
        
        return result.get('result')
    except requests.RequestException as e:
        raise Exception(f"Kanboard Connection Error: {e}")

def get_columns(user_id: int, project_id: int = None) -> List[Dict]:
    """Get all columns for the project."""
    pid = project_id or get_project_id(user_id)
    return _make_request(user_id, 'getColumns', {'project_id': pid})

def get_column_id_by_name(user_id: int, column_name: str, project_id: int = None) -> Optional[int]:
    """Find column ID by name."""
    columns = get_columns(user_id, project_id)
    for column in columns:
        if column.get('title', '').lower() == column_name.lower():
            return int(column['id'])
    return None

def get_tasks_by_column(user_id: int, column_id: int, project_id: int = None) -> List[Dict]:
    """Get all tasks from a specific column."""
    pid = project_id or get_project_id(user_id)
    all_tasks = _make_request(user_id, 'getAllTasks', {
        'project_id': pid,
        'status_id': 1  # Active tasks only
    })
    
    # Filter by column
    return [task for task in all_tasks if int(task.get('column_id', 0)) == column_id]

def get_task_tags(user_id: int, task_id: int) -> List[str]:
    """Get tags for a specific task."""
    tags = _make_request(user_id, 'getTaskTags', {'task_id': task_id})
    if isinstance(tags, dict):
        return list(tags.values())
    return tags or []

def parse_description(description: str) -> Dict[str, str]:
    """Parse task description to extract form fields."""
    result = {}
    if not description:
        return result
    
    field_mappings = {
        'vor- und nachname': 'vorname_nachname',
        'anschrift': 'rechnungsanschrift',
        'e-mail': 'email_address',
        'telefon': 'telefon',
        'name der veranstaltung': 'veranstaltungsname',
        'art der veranstaltung': 'veranstaltungsart',
        'veranstaltungsort': 'veranstaltungsort',
        'veranstaltungsbereich': 'veranstaltungsbereich',
        'erwartete personenzahl': 'personenzahl',
        'datum': 'veranstaltungsdatum',
        'benötigtes material': 'material',
        'was du uns sonst noch': 'sonstiges',
        'rahmenbedingungen': 'rahmenbedingungen',
    }
    
    lines = description.split('\n')
    for line in lines:
        line = line.strip()
        if ':' in line:
            parts = line.split(':', 1)
            if len(parts) == 2:
                label = parts[0].strip().lower()
                value = parts[1].strip()
                for desc_label, form_field in field_mappings.items():
                    if desc_label in label:
                        result[form_field] = value
                        break
    return result

def get_leihanfragen_tasks(user_id: int, project_id: int = None) -> List[Dict]:
    """Get all tasks from the 'Leihanfragen' column with parsed data."""
    pid = project_id or get_project_id(user_id)
    column_id = get_column_id_by_name(user_id, 'Leihanfrage', pid)
    if not column_id:
        return [] 
    
    tasks = get_tasks_by_column(user_id, column_id, pid)
    enriched_tasks = []
    for task in tasks:
        parsed_data = parse_description(task.get('description', ''))
        
        # Fetch tags for this task
        tags = []
        try:
            tags = get_task_tags(user_id, int(task.get('id')))
        except Exception:
            pass
        
        enriched_tasks.append({
            'id': task.get('id'),
            'title': task.get('title'),
            'description': task.get('description', ''),
            'date_due': task.get('date_due', ''),
            'tags': tags,
            'parsed_data': parsed_data
        })
    return enriched_tasks

def get_task_details(user_id: int, task_id: int) -> Dict:
    """Get full task details including tags."""
    task = _make_request(user_id, 'getTask', {'task_id': task_id})
    if not task:
        raise Exception(f"Task {task_id} nicht gefunden")
    
    parsed_data = parse_description(task.get('description', ''))
    tags = get_task_tags(user_id, task_id)
    
    return {
        'id': task.get('id'),
        'title': task.get('title'),
        'description': task.get('description', ''),
        'parsed_data': parsed_data,
        'tags': tags
    }

def _format_date_with_time(date_str: str) -> Optional[str]:
    """Format date string to YYYY-MM-DD 00:00 (Midnight)."""
    if not date_str:
        return None
    
    try:
        from datetime import datetime
        dt = None
        # 1. Strip time if present (to re-add clean 00:00)
        if 'T' in date_str:
            date_str = date_str.split('T')[0]
        if ' ' in date_str:
            date_str = date_str.split(' ')[0]

        # 2. Parse/Convert format
        if '.' in date_str:
             dt = datetime.strptime(date_str, '%d.%m.%Y')
        else:
             dt = datetime.strptime(date_str, '%Y-%m-%d')
             
        # 3. Return explicit YYYY-MM-DD 00:00
        return dt.strftime('%Y-%m-%d 00:00')
    except:
        return None

def create_task(user_id: int, title: str, description: str, due_date: str = None, 
                tags: List[str] = None, column_name: str = 'Leihanfrage', project_id: int = None) -> Dict:
    """Create a new task in Kanboard."""
    pid = project_id or get_project_id(user_id)
    column_id = get_column_id_by_name(user_id, column_name, pid)
    if not column_id:
        raise Exception(f"Spalte '{column_name}' nicht gefunden")
    
    params = {
        'project_id': pid,
        'title': title,
        'description': description,
        'column_id': column_id,
    }
    
    formatted_date = _format_date_with_time(due_date)
    if formatted_date:
        params['date_due'] = formatted_date
        params['date_started'] = formatted_date
    
    task_id = _make_request(user_id, 'createTask', params)
    if not task_id:
        raise Exception("Task konnte nicht erstellt werden")
    
    if tags:
        try:
            _make_request(user_id, 'setTaskTags', {
                'project_id': pid,
                'task_id': task_id,
                'tags': tags
            })
        except:
            pass
    
    return {'id': task_id, 'title': title}

def update_task(user_id: int, task_id: int, title: str = None, description: str = None, 
                due_date: str = None, tags: List[str] = None, project_id: int = None) -> bool:
    """Update an existing task in Kanboard."""
    pid = project_id or get_project_id(user_id)
    params = {
        'id': task_id,
        'title': title,
        'description': description,
    }
    params = {k: v for k, v in params.items() if v is not None}
    
    if due_date is not None:
        formatted_date = _format_date_with_time(due_date)
        if formatted_date:
            params['date_due'] = formatted_date
        # If valid calculation failed, we ignore update
            
    if params:
        result = _make_request(user_id, 'updateTask', params)
        if not result:
            return False
            
    if tags is not None:
        try:
            _make_request(user_id, 'setTaskTags', {
                'project_id': pid,
                'task_id': task_id,
                'tags': tags
            })
        except:
            pass
            
    return True

def get_all_tags(user_id: int, project_id: int = None) -> List[str]:
    """Get all available tags for the project."""
    pid = project_id or get_project_id(user_id)
    try:
        tags = _make_request(user_id, 'getAllTags', {'project_id': pid})
        if isinstance(tags, list):
            return [tag.get('name', '') for tag in tags if tag.get('name')]
        return []
    except:
        return []
