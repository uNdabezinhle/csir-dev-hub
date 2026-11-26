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
CREATE TABLE IF NOT EXISTS project_blueprints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name TEXT NOT NULL,
  spec TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(project_id, name));
CREATE TABLE IF NOT EXISTS provisioning_evidence (
  request_id TEXT PRIMARY KEY, blueprint_id INTEGER NOT NULL, record TEXT NOT NULL,
  workspace_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS workspace_execution_records (
  workspace_id TEXT PRIMARY KEY, record TEXT NOT NULL, submitted_by TEXT NOT NULL, submitted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS project_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, kind TEXT NOT NULL,
  subject TEXT NOT NULL, detail TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'submitted', reviewed_by TEXT, reviewed_at REAL, review_note TEXT);
CREATE TABLE IF NOT EXISTS workspace_requests (
  owner TEXT NOT NULL, name TEXT NOT NULL, template TEXT NOT NULL, project_id INTEGER,
  profile TEXT NOT NULL, cpu INTEGER NOT NULL, mem_gb INTEGER NOT NULL,
  created_at REAL NOT NULL, submitted REAL NOT NULL DEFAULT 0, seat_id INTEGER, request_id TEXT,
  PRIMARY KEY (owner, name));
"""

def connect():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    return c

_conn = connect()

@contextmanager
def tx(immediate=False):
    with _lock:
        try:
            if immediate:
                _conn.execute("BEGIN IMMEDIATE")
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
        from . import sdlc_store
        sdlc_store.init(c)
        c.execute("BEGIN IMMEDIATE")
        if "request_id" not in {row[1] for row in c.execute("PRAGMA table_info(workspace_requests)")}:
            c.execute("ALTER TABLE workspace_requests ADD COLUMN request_id TEXT")
        for item in CATALOG_SEED:
            c.execute("INSERT OR IGNORE INTO catalog(template, data, published, updated_by, updated_at) VALUES (?,?,?,?,?)",
                      (item["template"], json.dumps(item), 1 if item.get("published") else 0, "seed", time.time()))
            # non-destructive migration: add keys introduced in newer seeds (e.g. 'image') without touching edited values
            row = c.execute("SELECT data FROM catalog WHERE template=?", (item["template"],)).fetchone()
            d = json.loads(row[0]); new = {k: v for k, v in item.items() if k not in d and k != "published"}
            if new: c.execute("UPDATE catalog SET data=? WHERE template=?", (json.dumps({**d, **new}), item["template"]))

def get_setting(key, default=None):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default

def set_setting(key, value):
    with tx() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
