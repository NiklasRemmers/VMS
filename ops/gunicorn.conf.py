"""
Gunicorn Configuration for VMS
Production WSGI server settings.
"""
import multiprocessing
import os

# Server socket
bind = os.environ.get('GUNICORN_BIND', '127.0.0.1:8000')

# Worker processes
workers = int(os.environ.get('GUNICORN_WORKERS', multiprocessing.cpu_count() * 2 + 1))
worker_class = 'sync'
worker_connections = 1000
timeout = 120
keepalive = 5

# Logging
accesslog = os.environ.get('GUNICORN_ACCESS_LOG', '/var/log/vms/access.log')
errorlog = os.environ.get('GUNICORN_ERROR_LOG', '/var/log/vms/error.log')
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info')

# Process naming
proc_name = 'vms'

# Security
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# Server mechanics
preload_app = True
daemon = False


def post_fork(server, worker):
    """Dispose SQLAlchemy engine after fork so each worker gets its own connection pool,
    then start the daily Kanboard reconcile scheduler in this worker.

    Each worker starts its own scheduler; the job itself is guarded by a Postgres
    advisory lock so only one worker actually reconciles per run."""
    import vms.domain.database as database
    if database._engine is not None:
        database._engine.dispose()

    try:
        import vms.app as app_module
        app_module.init_scheduler()
    except Exception as e:
        server.log.warning(f"Scheduler init im Worker fehlgeschlagen: {e}")

# Development override
if os.environ.get('FLASK_ENV') == 'development':
    bind = '127.0.0.1:5000'
    workers = 1
    accesslog = '-'
    errorlog = '-'
    loglevel = 'debug'
    reload = True
