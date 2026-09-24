import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from flask import Flask, g, jsonify, request, send_file, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

from extract import extract_text, analyze_text, analyze_text_multi, vision_extract_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "bidvest_esg.db"))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "data", "uploads"))
CHECKLIST_JSON = os.path.join(BASE_DIR, "checklist_items.json")

BOOTSTRAP_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "bidvest-esg-admin")
MAX_UPLOAD_MB = 20

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

STATUSES = ["Not started", "In progress", "Collected", "Verified", "N/A"]
ENTRY_FREQUENCIES = ["Daily", "Weekly", "Monthly"]
KITCHEN_CATEGORIES = ["School", "Hospital", "Corporate", "Retirement Village", "Learning Academy", "Other"]
PILLARS = ["Environmental", "Social", "Governance"]

# Collapses the 74 checklist items' ~15 sub-categories into 6 plain groups for the
# simplified capture/dashboard surfaces site users see day to day. Framework codes
# (GRI/King/IFRS) and the full category breakdown remain available in Bidvest Admin
# and in generated reports — this mapping never touches stored data, display only.
CATEGORY_GROUPS = {
    "Energy & climate": "Energy",
    "Water": "Water",
    "Waste & food waste": "Waste",
    "Packaging & sourcing": "Waste",
    "Workforce": "People",
    "Training & development": "People",
    "Health & safety": "Safety",
    "Food safety & customer": "Safety",
    "Suppliers & community": "People",
    "Transformation & compliance": "Compliance",
    "Oversight & reporting": "Compliance",
    "Ethics & conduct": "Compliance",
    "Supply chain governance": "Compliance",
    "Data integrity & assurance": "Compliance",
}
CATEGORY_GROUP_ORDER = ["Waste", "Energy", "Water", "People", "Safety", "Compliance"]

# ---------------------------------------------------------------- helpers

def agg_method_for_unit(unit_text):
    """Classify a checklist item's reporting unit into how repeated entries roll up.
    Heuristic based on the first comma-separated token of the unit text — good enough
    to auto-sum clearly additive quantities (kg, kWh, litres, headcounts, Rand totals)
    and auto-average clearly intensity/ratio metrics (%, scores, rates), but a rough
    proxy for the ~74 free-text unit strings in the checklist. A per-hour wage RATE
    (e.g. "R per hour") is intentionally routed to 'latest', not summed, via the
    rate/per check below running before the bare-currency check."""
    u = (unit_text or "").lower()
    first_token = u.split(",")[0].strip()

    def has(word):
        return re.search(r"\b" + re.escape(word) + r"\b", first_token) is not None

    latest_markers = ["status", "date", "level", "name", "topic"]
    if (any(has(m) for m in latest_markers) or "y/n" in first_token) and "%" not in first_token:
        return "latest"
    if "per hour" in first_token or "per meal" in first_token or first_token.startswith("score"):
        return "latest" if "per" in first_token else "average"
    if "%" in first_token or has("rate") or has("score"):
        return "average"
    sum_markers = ["kg", "kwh", "litre", "l", "kl", "r", "number", "meals", "hours",
                   "tco2e", "count"]
    if any(has(m) for m in sum_markers):
        return "sum"
    return "latest"


NUMERIC_STRIP_RE = re.compile(r"[^0-9.\-]")


def parse_numeric(value_text):
    if value_text is None:
        return None
    s = str(value_text).strip()
    if not s:
        return None
    # take the first number-looking token so "1 240 kg" / "R 800" / "45%" all parse
    m = re.search(r"-?[0-9][0-9,]*\.?[0-9]*", s.replace(" ", ""))
    if not m:
        return None
    cleaned = NUMERIC_STRIP_RE.sub("", m.group(0).replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def period_bounds(d, frequency):
    """Return (start_date, end_date, period_key) for the period containing date d."""
    freq = (frequency or "").lower()
    if freq.startswith("month"):
        start = d.replace(day=1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = nxt - timedelta(days=1)
        key = start.strftime("%Y-%m")
    elif freq.startswith("quarter"):
        q = (d.month - 1) // 3
        start = date(d.year, q * 3 + 1, 1)
        end_month = q * 3 + 3
        nxt = (date(d.year, end_month, 28) + timedelta(days=4)).replace(day=1)
        end = nxt - timedelta(days=1)
        key = f"{d.year}-Q{q + 1}"
    elif freq.startswith("semi"):
        half = 1 if d.month <= 6 else 2
        start = date(d.year, 1 if half == 1 else 7, 1)
        end = date(d.year, 6, 30) if half == 1 else date(d.year, 12, 31)
        key = f"{d.year}-H{half}"
    else:  # annual / unspecified
        start = date(d.year, 1, 1)
        end = date(d.year, 12, 31)
        key = str(d.year)
    return start, end, key


def parse_date(s, default=None):
    if not s:
        return default or date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return default or date.today()


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS checklist_items (
            id TEXT PRIMARY KEY,
            pillar TEXT NOT NULL,
            category TEXT NOT NULL,
            data_point TEXT NOT NULL,
            unit TEXT,
            frequency TEXT,
            typical_source TEXT,
            framework_ref TEXT,
            agg_method TEXT NOT NULL DEFAULT 'latest',
            sort_order INTEGER
        );

        CREATE TABLE IF NOT EXISTS regions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            region_id INTEGER NOT NULL REFERENCES regions(id),
            category TEXT NOT NULL,
            created_at TEXT,
            UNIQUE(name, region_id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'site',
            unit_id INTEGER REFERENCES units(id),
            pin_hash TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL REFERENCES checklist_items(id),
            unit_id INTEGER NOT NULL REFERENCES units(id),
            entry_date TEXT NOT NULL,
            frequency TEXT NOT NULL,
            value_text TEXT,
            numeric_value REAL,
            notes TEXT,
            created_by INTEGER,
            created_by_name TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_item_unit ON entries(item_id, unit_id);

        CREATE TABLE IF NOT EXISTS period_flags (
            item_id TEXT NOT NULL,
            unit_id INTEGER NOT NULL,
            period_key TEXT NOT NULL,
            flag TEXT NOT NULL,
            set_by TEXT,
            set_at TEXT,
            PRIMARY KEY (item_id, unit_id, period_key)
        );

        CREATE TABLE IF NOT EXISTS unit_item_na (
            item_id TEXT NOT NULL,
            unit_id INTEGER NOT NULL,
            set_by TEXT,
            set_at TEXT,
            PRIMARY KEY (item_id, unit_id)
        );

        CREATE TABLE IF NOT EXISTS evidence_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            unit_id INTEGER NOT NULL,
            entry_id INTEGER REFERENCES entries(id),
            stored_name TEXT NOT NULL,
            original_name TEXT NOT NULL,
            filesize INTEGER,
            uploaded_by TEXT,
            uploaded_at TEXT
        );
        """
    )
    # Migration for databases created before pin_hash existed (ALTER TABLE has no
    # IF NOT EXISTS in SQLite) — safe to ignore if the column is already there.
    try:
        db.execute("ALTER TABLE users ADD COLUMN pin_hash TEXT")
    except sqlite3.OperationalError:
        pass
    db.commit()

    with open(CHECKLIST_JSON) as f:
        items = json.load(f)
    for idx, item in enumerate(items):
        db.execute(
            """INSERT OR IGNORE INTO checklist_items
               (id, pillar, category, data_point, unit, frequency, typical_source, framework_ref, agg_method, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"], item["pillar"], item["category"], item["data_point"],
                item.get("unit"), item.get("frequency"), item.get("typical_source"),
                item.get("framework_ref"), agg_method_for_unit(item.get("unit")), idx,
            ),
        )
    db.commit()

    # INSERT OR IGNORE (not a count-then-insert check) so concurrent gunicorn workers
    # racing to seed the bootstrap admin on first boot never raise IntegrityError.
    db.execute(
        """INSERT OR IGNORE INTO users (username, password_hash, name, role, unit_id, created_at)
           VALUES (?, ?, ?, 'admin', NULL, ?)""",
        (BOOTSTRAP_ADMIN_USERNAME, generate_password_hash(BOOTSTRAP_ADMIN_PASSWORD),
         "Bidvest Admin", datetime.now(timezone.utc).isoformat()),
    )
    db.commit()

    db.close()


init_db()


# ---------------------------------------------------------------- auth

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def require_login(admin_only=False):
    user = current_user()
    if not user:
        return None, (jsonify({"error": "Not logged in"}), 401)
    if admin_only and user["role"] != "admin":
        return None, (jsonify({"error": "Admin access required"}), 403)
    return user, None


def unit_ids_for(user):
    """Units this user may act on: all of them for admin, else just their own."""
    db = get_db()
    if user["role"] == "admin":
        return [r["id"] for r in db.execute("SELECT id FROM units").fetchall()]
    return [user["unit_id"]] if user["unit_id"] else []


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password", "")
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE lower(username)=?", (username,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Incorrect username or password"}), 401
    session["user_id"] = row["id"]
    unit = None
    if row["unit_id"]:
        u = db.execute("SELECT * FROM units WHERE id=?", (row["unit_id"],)).fetchone()
        unit = dict(u) if u else None
    return jsonify({"role": row["role"], "name": row["name"], "username": row["username"], "unit": unit})


@app.route("/api/pin-units")
def pin_units():
    """Public (no login) list of units that have at least one site PIN set, so the
    login screen can offer a unit picker before authenticating. Only id/name/category
    are exposed — nothing sensitive."""
    db = get_db()
    rows = db.execute(
        """SELECT DISTINCT u.id, u.name, u.category, r.name AS region_name
           FROM units u JOIN regions r ON r.id = u.region_id
           JOIN users us ON us.unit_id = u.id
           WHERE us.role = 'site' AND us.pin_hash IS NOT NULL
           ORDER BY r.name, u.name"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/login/pin", methods=["POST"])
def login_pin():
    """Fast unlock for site staff: pick your unit, enter the short PIN Bidvest Admin
    gave you — no username/password to remember away from a desk."""
    data = request.get_json(force=True, silent=True) or {}
    unit_id = data.get("unit_id")
    pin = (data.get("pin") or "").strip()
    if not unit_id or not pin:
        return jsonify({"error": "Unit and PIN are required"}), 400
    db = get_db()
    candidates = db.execute(
        "SELECT * FROM users WHERE unit_id=? AND role='site' AND pin_hash IS NOT NULL",
        (unit_id,),
    ).fetchall()
    match = next((r for r in candidates if check_password_hash(r["pin_hash"], pin)), None)
    if not match:
        return jsonify({"error": "Incorrect PIN for that unit"}), 401
    session["user_id"] = match["id"]
    u = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
    return jsonify({"role": match["role"], "name": match["name"], "username": match["username"],
                    "unit": dict(u) if u else None})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/session")
def whoami():
    user = current_user()
    if not user:
        return jsonify({"loggedIn": False})
    db = get_db()
    unit = None
    if user["unit_id"]:
        u = db.execute("SELECT * FROM units WHERE id=?", (user["unit_id"],)).fetchone()
        unit = dict(u) if u else None
    return jsonify({"loggedIn": True, "role": user["role"], "name": user["name"],
                    "username": user["username"], "unit": unit})


# ---------------------------------------------------------------- regions / units / users

@app.route("/api/regions", methods=["GET", "POST"])
def regions():
    db = get_db()
    if request.method == "GET":
        user, err = require_login()
        if err:
            return err
        rows = db.execute("SELECT * FROM regions ORDER BY name").fetchall()
        return jsonify([dict(r) for r in rows])
    user, err = require_login(admin_only=True)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Region name required"}), 400
    db.execute("INSERT OR IGNORE INTO regions (name) VALUES (?)", (name,))
    db.commit()
    rows = db.execute("SELECT * FROM regions ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/units", methods=["GET", "POST"])
def units():
    db = get_db()
    if request.method == "GET":
        user, err = require_login()
        if err:
            return err
        rows = db.execute(
            """SELECT u.*, r.name AS region_name FROM units u
               JOIN regions r ON r.id = u.region_id ORDER BY r.name, u.name"""
        ).fetchall()
        result = [dict(r) for r in rows]
        if user["role"] != "admin":
            result = [r for r in result if r["id"] == user["unit_id"]]
        return jsonify(result)
    user, err = require_login(admin_only=True)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    region_name = (data.get("region") or "").strip()
    category = data.get("category")
    if not name or not region_name or category not in KITCHEN_CATEGORIES:
        return jsonify({"error": "name, region and a valid category are required"}), 400
    db.execute("INSERT OR IGNORE INTO regions (name) VALUES (?)", (region_name,))
    region_id = db.execute("SELECT id FROM regions WHERE name=?", (region_name,)).fetchone()["id"]
    try:
        db.execute(
            "INSERT INTO units (name, region_id, category, created_at) VALUES (?, ?, ?, ?)",
            (name, region_id, category, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "A unit with that name already exists in that region"}), 409
    return jsonify({"ok": True})


@app.route("/api/users", methods=["GET", "POST"])
def users_endpoint():
    user, err = require_login(admin_only=True)
    if err:
        return err
    db = get_db()
    if request.method == "GET":
        rows = db.execute(
            """SELECT us.id, us.username, us.name, us.role, us.unit_id, u.name AS unit_name,
                      (us.pin_hash IS NOT NULL) AS has_pin
               FROM users us LEFT JOIN units u ON u.id = us.unit_id ORDER BY us.name"""
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    name = (data.get("name") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") if data.get("role") in ("admin", "site") else "site"
    unit_id = data.get("unit_id")
    pin = (data.get("pin") or "").strip()
    if not username or not name or len(password) < 6:
        return jsonify({"error": "username, name and a password of 6+ characters are required"}), 400
    if role == "site" and not unit_id:
        return jsonify({"error": "A site user must be assigned to a unit"}), 400
    if pin and (not pin.isdigit() or not (4 <= len(pin) <= 6)):
        return jsonify({"error": "PIN must be 4-6 digits"}), 400
    pin_hash = generate_password_hash(pin) if pin else None
    try:
        db.execute(
            """INSERT INTO users (username, password_hash, name, role, unit_id, pin_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, generate_password_hash(password), name, role, unit_id if role == "site" else None,
             pin_hash if role == "site" else None, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "That username is already taken"}), 409
    return jsonify({"ok": True})


@app.route("/api/meta")
def meta():
    user, err = require_login()
    if err:
        return err
    return jsonify({"kitchen_categories": KITCHEN_CATEGORIES, "pillars": PILLARS,
                     "entry_frequencies": ENTRY_FREQUENCIES,
                     "category_groups": CATEGORY_GROUPS, "category_group_order": CATEGORY_GROUP_ORDER,
                     "ai_vision_available": bool(os.environ.get("ANTHROPIC_API_KEY"))})


# ---------------------------------------------------------------- checklist / status

@app.route("/api/checklist")
def checklist():
    user, err = require_login()
    if err:
        return err
    db = get_db()
    rows = db.execute("SELECT * FROM checklist_items ORDER BY sort_order").fetchall()
    return jsonify([dict(r) for r in rows])


def compute_item_status(db, item, unit_id, today=None):
    today = today or date.today()
    start, end, period_key = period_bounds(today, item["frequency"])

    na = db.execute("SELECT 1 FROM unit_item_na WHERE item_id=? AND unit_id=?",
                     (item["id"], unit_id)).fetchone()
    if na:
        return {"status": "N/A", "period_key": period_key, "rollup_value": None, "entry_count": 0}

    flag = db.execute(
        "SELECT flag FROM period_flags WHERE item_id=? AND unit_id=? AND period_key=?",
        (item["id"], unit_id, period_key),
    ).fetchone()

    period_entries = db.execute(
        """SELECT * FROM entries WHERE item_id=? AND unit_id=? AND entry_date>=? AND entry_date<=?
           ORDER BY entry_date""",
        (item["id"], unit_id, start.isoformat(), end.isoformat()),
    ).fetchall()

    any_entry = db.execute(
        "SELECT 1 FROM entries WHERE item_id=? AND unit_id=? LIMIT 1", (item["id"], unit_id)
    ).fetchone()

    rollup_value = None
    numeric_vals = [e["numeric_value"] for e in period_entries if e["numeric_value"] is not None]
    if period_entries:
        if item["agg_method"] == "sum" and numeric_vals:
            rollup_value = round(sum(numeric_vals), 4)
        elif item["agg_method"] == "average" and numeric_vals:
            rollup_value = round(sum(numeric_vals) / len(numeric_vals), 4)
        else:
            rollup_value = period_entries[-1]["value_text"]

    if flag and flag["flag"] == "Verified" and period_entries:
        status = "Verified"
    elif period_entries:
        status = "Collected"
    elif any_entry:
        status = "In progress"
    else:
        status = "Not started"

    return {
        "status": status, "period_key": period_key, "period_start": start.isoformat(),
        "period_end": end.isoformat(), "rollup_value": rollup_value,
        "entry_count": len(period_entries), "agg_method": item["agg_method"],
    }


@app.route("/api/status")
def status_endpoint():
    user, err = require_login()
    if err:
        return err
    db = get_db()
    unit_id = request.args.get("unit_id", type=int)
    allowed = unit_ids_for(user)
    if unit_id is not None and unit_id not in allowed:
        return jsonify({"error": "Not permitted for that unit"}), 403
    target_units = [unit_id] if unit_id else allowed
    items = db.execute("SELECT * FROM checklist_items ORDER BY sort_order").fetchall()

    result = []
    for item in items:
        item = dict(item)
        per_unit = {}
        for uid in target_units:
            per_unit[uid] = compute_item_status(db, item, uid)
        # roll multiple units up for this item: worst-case status wins for the summary view
        order = {"N/A": 0, "Not started": 1, "In progress": 2, "Collected": 3, "Verified": 4}
        if per_unit:
            worst = min(per_unit.values(), key=lambda v: order[v["status"]])
            evidence_count = db.execute(
                "SELECT COUNT(*) c FROM evidence_files WHERE item_id=? AND unit_id IN (%s)" %
                ",".join("?" * len(target_units)),
                (item["id"], *target_units),
            ).fetchone()["c"]
        else:
            worst = {"status": "Not started", "period_key": None, "rollup_value": None, "entry_count": 0}
            evidence_count = 0
        item["status_summary"] = worst
        item["per_unit"] = {str(k): v for k, v in per_unit.items()}
        item["evidence_count"] = evidence_count
        item["category_group"] = CATEGORY_GROUPS.get(item["category"], "Other")
        result.append(item)
    return jsonify(result)


@app.route("/api/flags", methods=["POST"])
def set_flag():
    user, err = require_login(admin_only=True)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    item_id, unit_id = data.get("item_id"), data.get("unit_id")
    action = data.get("action")
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    if action == "mark_na":
        db.execute("INSERT OR REPLACE INTO unit_item_na (item_id, unit_id, set_by, set_at) VALUES (?,?,?,?)",
                   (item_id, unit_id, user["name"], now))
    elif action == "clear_na":
        db.execute("DELETE FROM unit_item_na WHERE item_id=? AND unit_id=?", (item_id, unit_id))
    elif action == "verify":
        item = db.execute("SELECT * FROM checklist_items WHERE id=?", (item_id,)).fetchone()
        _, _, period_key = period_bounds(date.today(), item["frequency"])
        db.execute(
            "INSERT OR REPLACE INTO period_flags (item_id, unit_id, period_key, flag, set_by, set_at) VALUES (?,?,?,?,?,?)",
            (item_id, unit_id, period_key, "Verified", user["name"], now),
        )
    else:
        return jsonify({"error": "Unknown action"}), 400
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- entry capture

@app.route("/api/entry", methods=["POST"])
def add_entry():
    user, err = require_login()
    if err:
        return err
    db = get_db()
    item_id = request.form.get("item_id") or (request.get_json(silent=True) or {}).get("item_id")
    unit_id = request.form.get("unit_id", type=int) or (request.get_json(silent=True) or {}).get("unit_id")
    frequency = request.form.get("frequency") or (request.get_json(silent=True) or {}).get("frequency")
    entry_date_s = request.form.get("entry_date") or (request.get_json(silent=True) or {}).get("entry_date")
    value_text = request.form.get("value_text") or (request.get_json(silent=True) or {}).get("value_text")
    notes = request.form.get("notes") or (request.get_json(silent=True) or {}).get("notes")

    if not item_id or not unit_id or frequency not in ENTRY_FREQUENCIES:
        return jsonify({"error": "item_id, unit_id and a valid frequency are required"}), 400
    if unit_id not in unit_ids_for(user):
        return jsonify({"error": "You are not assigned to that unit"}), 403

    item = db.execute("SELECT * FROM checklist_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Unknown item_id"}), 404
    unit = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
    if not unit:
        return jsonify({"error": "Unknown unit_id"}), 404

    entry_date = parse_date(entry_date_s)
    numeric_value = parse_numeric(value_text)
    now = datetime.now(timezone.utc).isoformat()

    cur = db.execute(
        """INSERT INTO entries (item_id, unit_id, entry_date, frequency, value_text, numeric_value,
                                 notes, created_by, created_by_name, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (item_id, unit_id, entry_date.isoformat(), frequency, value_text, numeric_value,
         notes, user["id"], user["name"], now),
    )
    entry_id = cur.lastrowid

    evidence_saved = None
    if "file" in request.files and request.files["file"].filename:
        file = request.files["file"]
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext in ALLOWED_EXTENSIONS:
            stored_name = f"{item_id}_{uuid.uuid4().hex}.{ext}"
            dest_dir = os.path.join(UPLOAD_DIR, item_id)
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, stored_name)
            file.save(dest_path)
            db.execute(
                """INSERT INTO evidence_files (item_id, unit_id, entry_id, stored_name, original_name,
                                                filesize, uploaded_by, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, unit_id, entry_id, stored_name, file.filename, os.path.getsize(dest_path),
                 user["name"], now),
            )
            evidence_saved = file.filename

    db.commit()

    status = compute_item_status(db, dict(item), unit_id)
    return jsonify({
        "ok": True,
        "confirmation": f"Recorded {item['id']} – {item['data_point']} for {unit['name']} "
                        f"({unit['category']}), {entry_date.strftime('%d %b %Y')}.",
        "evidence_saved": evidence_saved,
        "status": status,
    })


@app.route("/api/extract", methods=["POST"])
def extract_from_evidence():
    """Central capture point: reads an uploaded photo/PDF/doc OR a typed note and
    suggests what to fill in. With an item_id, behaves as before (one data point,
    one suggested value/date/notes). Without one, scans every checklist item's
    declared unit against the same text and returns every plausible match as a
    candidate entry — so one delivery note or utility bill can pre-fill several
    data points at once instead of forcing a data point to be picked first.
    Always best-effort — every result is a suggestion for the person to review,
    never trusted blind or auto-saved."""
    user, err = require_login()
    if err:
        return err
    db = get_db()

    item = None
    item_id = request.form.get("item_id")
    if item_id:
        row = db.execute("SELECT * FROM checklist_items WHERE id=?", (item_id,)).fetchone()
        if row:
            item = dict(row)

    has_file = "file" in request.files and request.files["file"].filename
    raw_text = request.form.get("text")
    want_ai = request.form.get("mode") == "ai"
    ai_used = False
    ai_unavailable = False

    if has_file:
        file = request.files["file"]
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({"error": "Unsupported file type"}), 400
        file_bytes = file.read()
        if len(file_bytes) > MAX_UPLOAD_MB * 1024 * 1024:
            return jsonify({"error": "File too large"}), 400
        source_name = file.filename
        text = ""
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if want_ai and api_key:
            try:
                text = vision_extract_text(file_bytes, ext, api_key)
                ai_used = bool(text.strip())
            except Exception:
                text = ""
        elif want_ai and not api_key:
            ai_unavailable = True
        if not text.strip():
            try:
                text = extract_text(file_bytes, ext)
            except Exception:
                text = ""
    elif raw_text and raw_text.strip():
        text = raw_text.strip()
        source_name = "typed note"
    else:
        return jsonify({"error": "No file or text provided"}), 400

    try:
        if item:
            result = analyze_text(text, source_name, item)
        else:
            all_items = [dict(r) for r in
                         db.execute("SELECT * FROM checklist_items ORDER BY sort_order").fetchall()]
            result = analyze_text_multi(text, source_name, all_items)
    except Exception:
        result = {
            "ok": False,
            "message": "Couldn't process this — please fill in the fields manually.",
            "value_text": None, "value_confidence": None, "entry_date": None,
            "notes": None, "candidates": [], "raw_text": "",
        }
    result["ai_used"] = ai_used
    if ai_unavailable:
        result["ai_unavailable"] = True
    return jsonify(result)


@app.route("/api/entries")
def list_entries():
    user, err = require_login()
    if err:
        return err
    db = get_db()
    item_id = request.args.get("item_id")
    unit_id = request.args.get("unit_id", type=int)
    allowed = unit_ids_for(user)
    q = "SELECT e.*, u.name AS unit_name FROM entries e JOIN units u ON u.id=e.unit_id WHERE 1=1"
    params = []
    if item_id:
        q += " AND e.item_id=?"
        params.append(item_id)
    if unit_id:
        if unit_id not in allowed:
            return jsonify({"error": "Not permitted"}), 403
        q += " AND e.unit_id=?"
        params.append(unit_id)
    elif allowed:
        q += " AND e.unit_id IN (%s)" % ",".join("?" * len(allowed))
        params.extend(allowed)
    else:
        return jsonify([])
    q += " ORDER BY e.entry_date DESC, e.id DESC LIMIT 200"
    rows = db.execute(q, params).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- evidence

ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "heic", "gif", "webp",
    "xlsx", "xls", "csv", "docx", "doc", "txt", "eml", "msg",
}


@app.route("/api/evidence")
def list_evidence():
    user, err = require_login()
    if err:
        return err
    item_id = request.args.get("item_id")
    unit_id = request.args.get("unit_id", type=int)
    db = get_db()
    allowed = unit_ids_for(user)
    q = "SELECT * FROM evidence_files WHERE item_id=?"
    params = [item_id]
    if unit_id:
        if unit_id not in allowed:
            return jsonify({"error": "Not permitted"}), 403
        q += " AND unit_id=?"
        params.append(unit_id)
    elif allowed:
        q += " AND unit_id IN (%s)" % ",".join("?" * len(allowed))
        params.extend(allowed)
    q += " ORDER BY id DESC"
    rows = db.execute(q, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/download/<int:file_id>")
def download(file_id):
    user, err = require_login()
    if err:
        return err
    db = get_db()
    row = db.execute("SELECT * FROM evidence_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    if row["unit_id"] not in unit_ids_for(user):
        return jsonify({"error": "Not permitted"}), 403
    path = os.path.join(UPLOAD_DIR, row["item_id"], row["stored_name"])
    if not os.path.exists(path):
        return jsonify({"error": "File missing on disk"}), 410
    return send_file(path, as_attachment=True, download_name=row["original_name"])


@app.route("/api/evidence/<int:file_id>", methods=["DELETE"])
def delete_evidence(file_id):
    user, err = require_login(admin_only=True)
    if err:
        return err
    db = get_db()
    row = db.execute("SELECT * FROM evidence_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(UPLOAD_DIR, row["item_id"], row["stored_name"])
    if os.path.exists(path):
        os.remove(path)
    db.execute("DELETE FROM evidence_files WHERE id=?", (file_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- alerts

def compute_alerts(db):
    """Units that should have reported a data point by now but haven't."""
    items = db.execute("SELECT * FROM checklist_items").fetchall()
    all_units = db.execute("SELECT * FROM units").fetchall()
    alerts = []
    today = date.today()
    for item in items:
        start, end, period_key = period_bounds(today, item["frequency"])
        total_days = max((end - start).days, 1)
        elapsed_pct = (today - start).days / total_days
        if elapsed_pct < 0.6:
            continue  # too early in the period to call it overdue
        for unit in all_units:
            st = compute_item_status(db, dict(item), unit["id"], today)
            if st["status"] in ("Not started", "In progress"):
                alerts.append({
                    "unit_id": unit["id"], "unit_name": unit["name"], "category": unit["category"],
                    "item_id": item["id"], "data_point": item["data_point"], "pillar": item["pillar"],
                    "frequency": item["frequency"], "period_key": period_key,
                    "status": st["status"], "days_into_period": (today - start).days,
                })
    return alerts


@app.route("/api/alerts")
def alerts_endpoint():
    user, err = require_login(admin_only=True)
    if err:
        return err
    return jsonify(compute_alerts(get_db()))


# ---------------------------------------------------------------- summary (dashboard cards)

def compute_summary(db, unit_ids=None):
    """Pillar/frequency tallies are counted per checklist ITEM (74 total, matching the
    original workbook) using each item's worst-case status across the units in scope —
    never multiplied by the number of units. Unit performance is the separate, per-unit
    view of the same 74 items."""
    items = db.execute("SELECT * FROM checklist_items").fetchall()
    units_rows = db.execute("SELECT * FROM units").fetchall()
    if unit_ids is not None:
        units_rows = [u for u in units_rows if u["id"] in unit_ids]
    unit_id_list = [u["id"] for u in units_rows]
    order = {"N/A": 0, "Not started": 1, "In progress": 2, "Collected": 3, "Verified": 4}

    by_pillar = {p: {s: 0 for s in STATUSES} for p in PILLARS}
    by_frequency = {}
    unit_scores = {u["id"]: {"unit_name": u["name"], "category": u["category"], **{s: 0 for s in STATUSES}}
                   for u in units_rows}

    for item in items:
        per_unit = {uid: compute_item_status(db, dict(item), uid) for uid in unit_id_list}
        item_status = min((v["status"] for v in per_unit.values()), key=lambda s: order[s]) if per_unit else "Not started"

        by_pillar[item["pillar"]][item_status] += 1
        f = by_frequency.setdefault(item["frequency"] or "Unspecified", {"total": 0, "not_collected": 0})
        f["total"] += 1
        if item_status not in ("Collected", "Verified"):
            f["not_collected"] += 1

        for uid, st in per_unit.items():
            unit_scores[uid][st["status"]] += 1

    pillar_summary = []
    total = {s: 0 for s in STATUSES}
    total_points = 0
    for pillar, counts in by_pillar.items():
        points = sum(counts.values())
        total_points += points
        denom = points - counts["N/A"]
        pct = round(100 * (counts["Collected"] + counts["Verified"]) / denom, 1) if denom else 0.0
        pillar_summary.append({"pillar": pillar, "data_points": points, **counts, "pct_complete": pct})
        for s in STATUSES:
            total[s] += counts[s]

    denom_total = total_points - total["N/A"]
    total_pct = round(100 * (total["Collected"] + total["Verified"]) / denom_total, 1) if denom_total else 0.0

    unit_performance = []
    for uid, us in unit_scores.items():
        pts = sum(us[s] for s in STATUSES)
        denom = pts - us["N/A"]
        pct = round(100 * (us["Collected"] + us["Verified"]) / denom, 1) if denom else 0.0
        unit_performance.append({"unit_id": uid, "pct_complete": pct, **us})
    unit_performance.sort(key=lambda u: u["pct_complete"])

    return {
        "pillars": pillar_summary,
        "total": {"data_points": total_points, **total, "pct_complete": total_pct},
        "frequency": [{"frequency": f, **v} for f, v in by_frequency.items()],
        "unit_performance": unit_performance,
        "total_items": len(items),
        "total_units": len(units_rows),
    }


@app.route("/api/summary")
def summary():
    user, err = require_login()
    if err:
        return err
    unit_ids = None if user["role"] == "admin" else unit_ids_for(user)
    return jsonify(compute_summary(get_db(), unit_ids))


# ---------------------------------------------------------------- reports

from reports import build_pdf_report, build_docx_report, build_xlsx_report  # noqa: E402


@app.route("/api/reports/<fmt>")
def generate_report(fmt):
    user, err = require_login(admin_only=True)
    if err:
        return err
    if fmt not in ("pdf", "docx", "xlsx"):
        return jsonify({"error": "Unsupported format"}), 400
    scope = request.args.get("scope", "all")
    if scope != "all" and scope not in PILLARS:
        return jsonify({"error": "Invalid scope"}), 400
    unit_id = request.args.get("unit_id", type=int)

    db = get_db()
    items = db.execute("SELECT * FROM checklist_items ORDER BY sort_order").fetchall()
    if scope != "all":
        items = [i for i in items if i["pillar"] == scope]
    units_rows = db.execute("SELECT u.*, r.name AS region_name FROM units u JOIN regions r ON r.id=u.region_id").fetchall()
    if unit_id:
        units_rows = [u for u in units_rows if u["id"] == unit_id]

    ctx = {
        "scope": scope, "items": [dict(i) for i in items], "units": [dict(u) for u in units_rows],
        "generated_at": datetime.now(timezone.utc), "db": db,
        "compute_item_status": compute_item_status,
        "summary": compute_summary(db, [u["id"] for u in units_rows] if unit_id else None),
    }

    fname_base = f"Bidvest_ESG_Report_{scope}_{datetime.now().strftime('%Y%m%d')}"
    if fmt == "xlsx":
        buf = build_xlsx_report(ctx)
        return send_file(buf, as_attachment=True, download_name=f"{fname_base}.xlsx",
                          mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt == "docx":
        buf = build_docx_report(ctx)
        return send_file(buf, as_attachment=True, download_name=f"{fname_base}.docx",
                          mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    buf = build_pdf_report(ctx)
    return send_file(buf, as_attachment=True, download_name=f"{fname_base}.pdf", mimetype="application/pdf")


# ---------------------------------------------------------------- static / health

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
