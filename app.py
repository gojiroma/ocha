import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2
from flask import Flask, Response, jsonify, redirect, request, render_template, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB request body cap

# Environment configuration
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")
CRON_SECRET = os.environ.get("CRON_SECRET")

JST = timezone(timedelta(hours=9))

# Regex patterns
SYNC_ID_RE = re.compile(r"^[0-9a-f]{64}$")

# Schema for dictionary + mutterings app
OUCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ouch_dict_entries (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    reading TEXT,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_title ON ouch_dict_entries (title);
CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_reading ON ouch_dict_entries (reading);
CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_updated_at ON ouch_dict_entries (updated_at);

CREATE TABLE IF NOT EXISTS ouch_dict_entries_history (
    id SERIAL PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES ouch_dict_entries(id),
    title TEXT NOT NULL,
    reading TEXT,
    content TEXT NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_history_entry_id ON ouch_dict_entries_history (entry_id);
CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_history_archived_at ON ouch_dict_entries_history (archived_at);

CREATE TABLE IF NOT EXISTS ouch_mutterings (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_mutterings_created_at ON ouch_mutterings (created_at);

CREATE TABLE IF NOT EXISTS ouch_sync_state (
    sync_id TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    iv TEXT NOT NULL,
    content_updated_at TIMESTAMPTZ NOT NULL,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_sync_state_last_synced_at ON ouch_sync_state (last_synced_at);

CREATE TABLE IF NOT EXISTS ouch_export_flags (
    sync_id TEXT PRIMARY KEY,
    last_export_date TEXT NOT NULL,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_export_flags_last_synced_at ON ouch_export_flags (last_synced_at);
"""

def get_conn():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    raise RuntimeError("DATABASE_URL is not set")

def ensure_ouch_schema(conn):
    with conn.cursor() as cur:
        cur.execute(OUCH_SCHEMA_SQL)
    conn.commit()

def init_db():
    """Initialize schema"""
    try:
        with get_conn() as conn:
            ensure_ouch_schema(conn)
    except Exception as e:
        print(f"[init_db] schema initialization failed: {e}")

if DATABASE_URL:
    try:
        init_db()
    except Exception as e:
        print(f"[init_db] schema initialization failed: {e}")

# Authentication utilities
def classify_token(token):
    if not token:
        return None
    if ADMIN_TOKEN and secrets.compare_digest(token, ADMIN_TOKEN):
        return "admin"
    return None

def require_access(admin_only=False):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not ADMIN_TOKEN:
                return jsonify({"error": "server not configured: ADMIN_TOKEN is not set"}), 503
            supplied = request.headers.get("X-Access-Token") or request.args.get("token") or ""
            role = classify_token(supplied)
            if role is None:
                return jsonify({"error": "access token required"}), 401
            if admin_only and role != "admin":
                return jsonify({"error": "admin token required"}), 403
            request.access_role = role
            return view(*args, **kwargs)
        return wrapped
    return decorator

# Utility functions
def parse_iso8601(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def to_utc_iso8601(dt):
    return dt.astimezone(timezone.utc).isoformat()

# ============================================================================
# Frontend routes
# ============================================================================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/edit")
def edit_redirect():
    return redirect("/")

@app.route("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")

@app.route("/icons/<path:filename>")
def serve_icons(filename):
    return send_from_directory(os.path.join(app.static_folder, "icons"), filename)

# ============================================================================
# Sync API endpoints (for LocalStorage + Neon synchronization)
# ============================================================================

@app.route("/api/push", methods=["POST", "DELETE"])
def push():
    if request.method == "DELETE":
        sync_id = request.args.get("sync_id", "")
        if not SYNC_ID_RE.match(sync_id):
            return jsonify(error="sync_id must be a 64-character hex string"), 400
        try:
            conn = get_conn()
        except RuntimeError as err:
            return jsonify(error=str(err)), 500
        try:
            ensure_ouch_schema(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ouch_sync_state WHERE sync_id = %s", (sync_id,))
                deleted = cur.rowcount
            conn.commit()
            return jsonify(deleted=deleted)
        finally:
            conn.close()

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="invalid JSON body"), 400

    sync_id = body.get("sync_id")
    ciphertext = body.get("ciphertext")
    iv = body.get("iv")
    updated_at_raw = body.get("updated_at")

    if not (isinstance(sync_id, str) and SYNC_ID_RE.match(sync_id)):
        return jsonify(error="sync_id must be a 64-character hex string"), 400
    if not (isinstance(ciphertext, str) and ciphertext):
        return jsonify(error="ciphertext is required"), 400
    if not (isinstance(iv, str) and iv):
        return jsonify(error="iv is required"), 400
    try:
        updated_at = parse_iso8601(updated_at_raw)
    except (TypeError, ValueError):
        return jsonify(error="updated_at must be an ISO 8601 timestamp"), 400
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    try:
        conn = get_conn()
    except RuntimeError as err:
        return jsonify(error=str(err)), 500

    try:
        ensure_ouch_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content_updated_at FROM ouch_sync_state WHERE sync_id = %s",
                (sync_id,),
            )
            row = cur.fetchone()
            server_updated_at = row[0] if row else None

            if server_updated_at is not None and server_updated_at >= updated_at:
                cur.execute(
                    "UPDATE ouch_sync_state SET last_synced_at = now() WHERE sync_id = %s",
                    (sync_id,),
                )
                conn.commit()
                return jsonify(
                    applied=False,
                    updated_at=to_utc_iso8601(server_updated_at),
                )

            cur.execute(
                """
                INSERT INTO ouch_sync_state (sync_id, ciphertext, iv, content_updated_at, last_synced_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (sync_id) DO UPDATE SET
                    ciphertext = EXCLUDED.ciphertext,
                    iv = EXCLUDED.iv,
                    content_updated_at = EXCLUDED.content_updated_at,
                    last_synced_at = now()
                """,
                (sync_id, ciphertext, iv, updated_at),
            )
        conn.commit()
        return jsonify(applied=True, updated_at=to_utc_iso8601(updated_at))
    finally:
        conn.close()

@app.route("/api/pull", methods=["GET"])
def pull():
    sync_id = request.args.get("sync_id", "")
    if not SYNC_ID_RE.match(sync_id):
        return jsonify(error="sync_id must be a 64-character hex string"), 400

    try:
        conn = get_conn()
    except RuntimeError as err:
        return jsonify(error=str(err)), 500

    try:
        ensure_ouch_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ciphertext, iv, content_updated_at FROM ouch_sync_state WHERE sync_id = %s",
                (sync_id,),
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return jsonify(found=False)

            cur.execute(
                "UPDATE ouch_sync_state SET last_synced_at = now() WHERE sync_id = %s",
                (sync_id,),
            )
        conn.commit()
        ciphertext, iv, content_updated_at = row
        return jsonify(
            found=True,
            ciphertext=ciphertext,
            iv=iv,
            updated_at=to_utc_iso8601(content_updated_at),
        )
    finally:
        conn.close()

@app.route("/api/export-flag", methods=["POST"])
def export_flag():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="invalid JSON body"), 400

    sync_id = body.get("sync_id")
    if not (isinstance(sync_id, str) and SYNC_ID_RE.match(sync_id)):
        return jsonify(error="sync_id must be a 64-character hex string"), 400

    today_jst = datetime.now(JST).strftime("%Y-%m-%d")

    try:
        conn = get_conn()
    except RuntimeError as err:
        return jsonify(error=str(err)), 500

    try:
        ensure_ouch_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ouch_export_flags (sync_id, last_export_date, last_synced_at)
                VALUES (%s, %s, now())
                ON CONFLICT (sync_id) DO UPDATE SET
                    last_export_date = EXCLUDED.last_export_date,
                    last_synced_at = now()
                WHERE ouch_export_flags.last_export_date IS DISTINCT FROM EXCLUDED.last_export_date
                RETURNING last_export_date
                """,
                (sync_id, today_jst),
            )
            claimed = cur.fetchone() is not None
        conn.commit()
        return jsonify(claimed=claimed, date=today_jst)
    finally:
        conn.close()

@app.route("/api/cleanup", methods=["GET"])
def cleanup():
    if not CRON_SECRET:
        return jsonify(error="CRON_SECRET is not configured"), 500

    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {CRON_SECRET}":
        return jsonify(error="unauthorized"), 401

    try:
        conn = get_conn()
    except RuntimeError as err:
        return jsonify(error=str(err)), 500

    try:
        ensure_ouch_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ouch_sync_state WHERE last_synced_at < now() - interval '7 days'"
            )
            deleted = cur.rowcount

            cur.execute(
                "DELETE FROM ouch_export_flags WHERE last_synced_at < now() - interval '7 days'"
            )
            deleted_export_flags = cur.rowcount
        conn.commit()
        return jsonify(
            deleted=deleted,
            deleted_export_flags=deleted_export_flags,
        )
    finally:
        conn.close()

# ============================================================================
# Dictionary API endpoints
# ============================================================================

def row_to_dict_entry(row):
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "reading": row["reading"],
        "content": row["content"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }

@app.route("/api/dict", methods=["GET"])
@require_access()
def list_dict_entries():
    search = request.args.get("search", "")
    limit = min(int(request.args.get("limit", 100)), 1000)

    with get_conn() as conn:
        with conn.cursor() as cur:
            if search:
                search_pattern = f"%{search}%"
                cur.execute(
                    """SELECT * FROM ouch_dict_entries
                    WHERE title ILIKE %s OR reading ILIKE %s OR content ILIKE %s
                    ORDER BY updated_at DESC LIMIT %s""",
                    (search_pattern, search_pattern, search_pattern, limit),
                )
            else:
                cur.execute("SELECT * FROM ouch_dict_entries ORDER BY updated_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()

    return jsonify([row_to_dict_entry(r) for r in rows])

@app.route("/api/dict", methods=["POST"])
@require_access(admin_only=True)
def create_dict_entry():
    data = request.get_json(force=True, silent=True) or {}
    title = data.get("title")
    reading = data.get("reading")
    content = data.get("content")

    if not title or not content:
        return jsonify({"error": "title and content are required"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ouch_dict_entries (title, reading, content)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (title, reading, content),
            )
            row = cur.fetchone()
        conn.commit()

    return jsonify(row_to_dict_entry(row)), 201

@app.route("/api/dict/<entry_id>", methods=["GET"])
@require_access()
def get_dict_entry(entry_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ouch_dict_entries WHERE id = %s", (entry_id,))
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict_entry(row))

@app.route("/api/dict/<entry_id>", methods=["PUT"])
@require_access(admin_only=True)
def update_dict_entry(entry_id):
    data = request.get_json(force=True, silent=True) or {}
    fields = []
    values = []

    if "title" in data:
        fields.append("title = %s")
        values.append(data["title"])
    if "reading" in data:
        fields.append("reading = %s")
        values.append(data["reading"])
    if "content" in data:
        fields.append("content = %s")
        values.append(data["content"])

    if not fields:
        return jsonify({"error": "no fields to update"}), 400

    fields.append("updated_at = now()")
    values.append(entry_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE ouch_dict_entries SET {', '.join(fields)} WHERE id = %s RETURNING *",
                values,
            )
            row = cur.fetchone()
        conn.commit()

    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict_entry(row))

@app.route("/api/dict/<entry_id>", methods=["DELETE"])
@require_access(admin_only=True)
def delete_dict_entry(entry_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ouch_dict_entries WHERE id = %s RETURNING id", (entry_id,))
            deleted = cur.fetchone()
        conn.commit()

    if not deleted:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})

@app.route("/api/dict/<entry_id>/history", methods=["GET"])
@require_access()
def get_dict_entry_history(entry_id):
    limit = min(int(request.args.get("limit", 50)), 200)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, entry_id, title, reading, content, archived_at
                FROM ouch_dict_entries_history
                WHERE entry_id = %s
                ORDER BY archived_at DESC
                LIMIT %s
                """,
                (entry_id, limit),
            )
            rows = cur.fetchall()

    return jsonify([
        {
            "id": str(r["id"]),
            "entry_id": str(r["entry_id"]),
            "title": r["title"],
            "reading": r["reading"],
            "content": r["content"],
            "archived_at": r["archived_at"].isoformat(),
        }
        for r in rows
    ])

# ============================================================================
# Mutterings API endpoints
# ============================================================================

def row_to_muttering(row):
    return {
        "id": str(row["id"]),
        "content": row["content"],
        "created_at": row["created_at"].isoformat(),
    }

@app.route("/api/mutterings", methods=["GET"])
@require_access()
def list_mutterings():
    search = request.args.get("search", "")
    limit = min(int(request.args.get("limit", 100)), 1000)

    with get_conn() as conn:
        with conn.cursor() as cur:
            if search:
                search_pattern = f"%{search}%"
                cur.execute(
                    """SELECT * FROM ouch_mutterings
                    WHERE content ILIKE %s
                    ORDER BY created_at DESC LIMIT %s""",
                    (search_pattern, limit),
                )
            else:
                cur.execute("SELECT * FROM ouch_mutterings ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()

    return jsonify([row_to_muttering(r) for r in rows])

@app.route("/api/mutterings", methods=["POST"])
@require_access(admin_only=True)
def create_muttering():
    data = request.get_json(force=True, silent=True) or {}
    content = data.get("content")

    if not content:
        return jsonify({"error": "content is required"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ouch_mutterings (content) VALUES (%s) RETURNING *",
                (content,),
            )
            row = cur.fetchone()
        conn.commit()

    return jsonify(row_to_muttering(row)), 201

@app.route("/api/mutterings/<muttering_id>", methods=["DELETE"])
@require_access(admin_only=True)
def delete_muttering(muttering_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ouch_mutterings WHERE id = %s RETURNING id", (muttering_id,))
            deleted = cur.fetchone()
        conn.commit()

    if not deleted:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})

# ============================================================================
# Auth check endpoint
# ============================================================================

@app.route("/api/auth/check", methods=["GET"])
@require_access()
def auth_check():
    return jsonify({"ok": True, "role": request.access_role})

if __name__ == "__main__":
    app.run(debug=True, port=5001)
