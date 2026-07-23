"""
Kanboard API Client
Handles communication with the Kanboard TODO board
Supports per-user configuration.
"""
import requests
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from vms.domain.database import get_user_settings
from vms.infra.security import decrypt_value

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo('Europe/Berlin')
except Exception:  # pragma: no cover - fallback if tzdata missing
    LOCAL_TZ = None

from vms.domain.models import CandidateStatus, TERMINAL_STATUSES, parse_flexible_date, to_iso_date

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_ID = 25  # Fallback

# ─── Kanboard column names per candidate lifecycle state ───
# Resolved by name at runtime (case-insensitive). These columns must exist
# in the Kanboard project with exactly these titles.
COLUMN_PROCESSED = 'Leihanfrage'
COLUMN_DONE      = 'Abholdatum, Referent festgelegt oder Leihvertrag gemacht'
COLUMN_VERLIEHEN = 'Verliehen'
COLUMN_INVOICE   = 'Zurückgegeben/Rechnung offen'

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

# Canonical (VMS field -> Kanboard label) mapping. The inverse of
# parse_description's field_mappings: build_task_description renders these labels,
# parse_description reads them back. Kept ordered so the generated description is
# stable. (parse_description stays substring-based; unifying both onto this list
# is out of scope -- see docs/specs/kanboard-sync-vms-truth.md.)
_TASK_DESCRIPTION_FIELDS = [
    ('vorname_nachname', 'Vor- und Nachname'),
    ('anschrift', 'Anschrift'),
    ('email_address', 'E-Mail'),
    ('telefon', 'Telefon'),
    ('veranstaltungsname', 'Name der Veranstaltung'),
    ('veranstaltungsart', 'Art der Veranstaltung'),
    ('veranstaltungsort', 'Veranstaltungsort'),
    ('veranstaltungsbereich', 'Veranstaltungsbereich'),
    ('personenzahl', 'Erwartete Personenzahl'),
    ('material', 'Benötigtes Material'),
    ('sonstiges', 'Was du uns sonst noch mitteilen möchtest'),
    ('rahmenbedingungen', 'Rahmenbedingungen'),
]


def build_task_description(fields: Dict[str, Any]) -> str:
    """Render VMS candidate fields into a Kanboard task description.

    Inverse of parse_description: one 'Label: value' line per non-empty field,
    using labels parse_description reads back verbatim. Empty/None fields are
    omitted so the round-trip stays stable."""
    lines = []
    for field, label in _TASK_DESCRIPTION_FIELDS:
        value = fields.get(field)
        if value:
            lines.append(f"{label}: {value}")
    return '\n'.join(lines)


def kanboard_date_due_to_iso(date_due) -> str:
    """Convert a Kanboard date_due (Unix-timestamp string) to ISO 'YYYY-MM-DD'.

    Empty or '0' -> ''. A midnight timestamp is interpreted in Europe/Berlin so
    the calendar day is correct. Non-numeric input falls back to to_iso_date,
    which normalizes what it can and returns the input verbatim otherwise."""
    if not date_due or date_due == '0':
        return ''
    try:
        ts = int(date_due)
        if LOCAL_TZ is not None:
            return datetime.fromtimestamp(ts, tz=LOCAL_TZ).strftime('%Y-%m-%d')
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except (ValueError, OSError):
        return to_iso_date(date_due)


def plan_task_push(candidate: Dict[str, Any], task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compare the VMS truth against a Kanboard task and return the update_task
    kwargs to push (title, description, due_date, tags), or None if already in sync.

    An empty VMS datum never triggers a date push, so a date already set in
    Kanboard is not cleared."""
    title = candidate.get('veranstaltungsname') or candidate.get('subject')
    description = build_task_description(candidate)
    tags = candidate.get('tags') or []
    datum = candidate.get('datum') or ''

    diverged = (
        title != task.get('title')
        or description != (task.get('description') or '')
        or set(tags) != set(task.get('tags') or [])
        or (bool(datum) and datum != kanboard_date_due_to_iso(task.get('date_due')))
    )
    if not diverged:
        return None
    return {'title': title, 'description': description, 'due_date': datum, 'tags': tags}


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
            logger.warning("Tags für Task %s nicht abrufbar; Task wird ohne Tags "
                           "dargestellt", task.get('id'), exc_info=True)
        
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
    """Format a date string to Kanboard's "YYYY-MM-DD 00:00" (midnight)."""
    d = parse_flexible_date(date_str)
    if d is None:
        if date_str:
            # Der Aufrufer legt den Task sonst ohne Fälligkeitsdatum an,
            # ohne es zu merken.
            logger.warning("Fälligkeitsdatum %r nicht interpretierbar; Task "
                           "erhält kein Datum", date_str)
        return None
    return d.strftime('%Y-%m-%d 00:00')


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
        except Exception:
            logger.warning("setTaskTags für neuen Task %s fehlgeschlagen; der Task "
                           "existiert, die Tags %r sind verloren", task_id, tags,
                           exc_info=True)

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
        except Exception:
            logger.warning("setTaskTags für Task %s fehlgeschlagen; die Tags %r "
                           "wurden nicht übernommen", task_id, tags, exc_info=True)

    return True

def get_all_tags(user_id: int, project_id: int = None) -> List[str]:
    """Get all available tags for the project."""
    pid = project_id or get_project_id(user_id)
    try:
        tags = _make_request(user_id, 'getAllTags', {'project_id': pid})
        if isinstance(tags, list):
            return [tag.get('name', '') for tag in tags if tag.get('name')]
        return []
    except Exception:
        # Sonst ist "es gibt keine Tags" nicht von "Kanboard ist nicht erreichbar"
        # zu unterscheiden -- die UI zeigt in beiden Fällen eine leere Liste.
        logger.warning("getAllTags für Projekt %s fehlgeschlagen; UI zeigt keine Tags",
                       pid, exc_info=True)
        return []


def _parse_date(date_str: str):
    """Alias auf die kanonische Datumslogik -- hier nur erhalten, weil mehrere
    Aufrufer im Repo diesen Namen benutzen."""
    return parse_flexible_date(date_str)


def _today():
    """Local (Europe/Berlin) current date, matching the daily reconcile job."""
    if LOCAL_TZ is not None:
        return datetime.now(LOCAL_TZ).date()
    return datetime.now().date()


def move_task(user_id: int, task_id: int, column_name: str, position: int = 1,
              project_id: int = None) -> bool:
    """Move a task to another column (by name). No-op if it is already there.

    Returns True if the task is in (or was moved to) the target column,
    False if the column could not be resolved."""
    pid = project_id or get_project_id(user_id)

    task = _make_request(user_id, 'getTask', {'task_id': task_id})
    if not task:
        logger.warning("move_task: Kanboard task %s nicht gefunden", task_id)
        return False

    target_col_id = get_column_id_by_name(user_id, column_name, pid)
    if not target_col_id:
        logger.warning("move_task: Spalte '%s' nicht gefunden (Projekt %s)", column_name, pid)
        return False

    if int(task.get('column_id', 0)) == target_col_id:
        return True  # already in the right column

    swimlane_id = int(task.get('swimlane_id') or 1)
    _make_request(user_id, 'moveTaskPosition', {
        'project_id': pid,
        'task_id': task_id,
        'column_id': target_col_id,
        'position': position,
        'swimlane_id': swimlane_id,
    })
    return True


def close_task(user_id: int, task_id: int) -> bool:
    """Close a task in Kanboard. No-op if it is already closed."""
    task = _make_request(user_id, 'getTask', {'task_id': task_id})
    if not task:
        logger.warning("close_task: Kanboard task %s nicht gefunden", task_id)
        return False
    if int(task.get('is_active', 1)) == 0:
        return True  # already closed
    return bool(_make_request(user_id, 'closeTask', {'task_id': task_id}))


def reconcile_candidate(user_id: int, candidate: Dict) -> None:
    """Move/close the linked Kanboard task to match the candidate's lifecycle state.

    Best-effort: any Kanboard error is logged and swallowed so it never breaks
    the calling request. Candidates without a kanboard_task_id are ignored."""
    task_id = candidate.get('kanboard_task_id')
    if not task_id:
        return

    status = candidate.get('status')
    try:
        if status == CandidateStatus.PROCESSED:
            move_task(user_id, task_id, COLUMN_PROCESSED)
        elif status == CandidateStatus.DONE:
            start = _parse_date(candidate.get('datum'))
            if start is not None and _today() >= start:
                move_task(user_id, task_id, COLUMN_VERLIEHEN)
            else:
                move_task(user_id, task_id, COLUMN_DONE)
        elif status in TERMINAL_STATUSES:
            close_task(user_id, task_id)
        elif status == CandidateStatus.INVOICE_PENDING:
            move_task(user_id, task_id, COLUMN_INVOICE)
        # 'pending' → no Kanboard task yet, nothing to do
    except Exception as e:
        logger.warning("reconcile_candidate: Kanboard-Abgleich für Task %s fehlgeschlagen: %s",
                       task_id, e)


def reconcile_candidate_by_id(user_id: int, candidate_id: int) -> None:
    """Lade einen Vorgang und gleiche seinen Kanboard-Task mit dem Status ab.

    Lag vorher als app._reconcile_kanboard im App-Modul, weshalb invoice_routes
    per `from app import ...` zurückimportieren musste. Der Import von
    email_client bleibt funktionslokal: email_client importiert seinerseits
    dieses Modul, ein Modul-Import wäre zirkulär.

    Best effort -- wirft nie in den aufrufenden Request, meldet den Fehlschlag
    aber im Log (früher schluckten ihn zwei der drei Aufrufstellen spurlos).
    """
    from vms.clients.email_client import get_candidate_by_id
    try:
        candidate = get_candidate_by_id(candidate_id)
        if candidate:
            reconcile_candidate(user_id, candidate)
    except Exception:
        logger.warning("Kanboard-Abgleich für Vorgang %s fehlgeschlagen",
                       candidate_id, exc_info=True)
