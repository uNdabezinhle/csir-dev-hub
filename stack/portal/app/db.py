"""SQLite persistence for the portal (users, projects, quotas, catalog, secrets, workspace ledger, audit)."""
import sqlite3, threading, json, time
from contextlib import contextmanager
from .config import DATA_DIR, CATALOG_SEED

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "devhub.db"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, email TEXT, full_name TEXT, auth_source TEXT NOT NULL,  -- local | oidc
  password_hash TEXT, roles TEXT NOT NULL DEFAULT '[]', disabled INTEGER DEFAULT 0,
  created_at REAL, last_login REAL);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, owner TEXT, cost_centre TEXT,
  max_workspaces INTEGER, max_cpu INTEGER, max_mem_gb INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS project_members (project_id INTEGER, username TEXT, PRIMARY KEY(project_id, username));
CREATE TABLE IF NOT EXISTS user_quotas (
  username TEXT PRIMARY KEY, max_workspaces INTEGER, max_cpu INTEGER, max_mem_gb INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS catalog (
  template TEXT PRIMARY KEY, data TEXT NOT NULL, published INTEGER, updated_by TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS secrets (
  username TEXT, name TEXT, value_enc TEXT, updated_at REAL, PRIMARY KEY(username, name));
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, owner TEXT, name TEXT, template TEXT, project_id INTEGER, profile TEXT,
  cpu INTEGER, mem_gb INTEGER, created_by TEXT, created_at REAL, deleted_at REAL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, target TEXT, detail TEXT, ip TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

def connect():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    return c

_conn = connect()

@contextmanager
def tx():
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback(); raise

def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]

def one(sql, args=()):
    r = q(sql, args); return r[0] if r else None

def init():
    with tx() as c:
        c.executescript(SCHEMA)
        for item in CATALOG_SEED:
            c.execute("INSERT OR IGNORE INTO catalog(template, data, published, updated_by, updated_at) VALUES (?,?,?,?,?)",
                      (item["template"], json.dumps(item), 1 if item.get("published") else 0, "seed", time.time()))

def get_setting(key, default=None):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default

def set_setting(key, value):
    with tx() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
