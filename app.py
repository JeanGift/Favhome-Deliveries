# app.py — FavHome Deliveries (safe GitHub-backed SQLite sync on boot + after-writes)
import threading
import requests
import os
import sqlite3
import html
import base64
import requests
import hashlib
import time
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, abort
from pathlib import Path
from werkzeug.utils import secure_filename

# ----------------------- CONFIG -----------------------
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # format: owner/repo
GITHUB_DB_PATH = os.getenv("GITHUB_DB_PATH", "orders.db")  # path inside repo
GITHUB_API_CONTENTS = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_DB_PATH}"
GITHUB_API_COMMITS = f"https://api.github.com/repos/{GITHUB_REPO}/commits"

app = Flask(__name__, static_folder='.', template_folder='.')
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_key_here')

DB_PATH = 'orders.db'
# default ADMIN_KEY may be overridden by env var OR the admin password stored in app.db
ADMIN_KEY = os.getenv('FAVHOME_ADMIN_KEY', 'change_me')
PAYBILL = os.getenv('FAVHOME_PAYBILL', '400200')

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# ----------------------- UTILITIES -----------------------


def keep_awake():
    """Background thread to ping self every 30-40 seconds."""
    import time
    url = f"http://localhost:{os.environ.get('PORT', 5000)}/ping"
    while True:
        try:
            requests.get(url, timeout=5)
        except:
            pass
        time.sleep(35)  # ping every 35 seconds (Render wakes at ~55s inactivity)

# Start keep-awake thread
threading.Thread(target=keep_awake, daemon=True).start()

def _headers():
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

def _sha256_of_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def get_remote_file_info():
    """
    Returns tuple (status_code, content_bytes_or_none, sha_or_none, commit_date_iso_or_none)
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None, None, None, None

    try:
        r = requests.get(GITHUB_API_CONTENTS, headers=_headers(), timeout=15)
    except Exception as e:
        print("get_remote_file_info: request failed:", e)
        return None, None, None, None

    if r.status_code != 200:
        return r.status_code, None, None, None

    j = r.json()
    encoded = j.get("content", "")
    sha = j.get("sha")
    try:
        content = base64.b64decode(encoded)
    except Exception:
        content = None

    # Get latest commit date for path
    commit_date = None
    try:
        q = {"path": GITHUB_DB_PATH, "per_page": 1}
        rc = requests.get(GITHUB_API_COMMITS, headers=_headers(), params=q, timeout=15)
        if rc.status_code == 200 and isinstance(rc.json(), list) and len(rc.json()) > 0:
            commit_date = rc.json()[0]["commit"]["committer"]["date"]
    except Exception as e:
        print("get_remote_file_info: commit lookup failed:", e)

    return r.status_code, content, sha, commit_date

def upload_bytes_to_github(path_in_repo: str, content_bytes: bytes, message: str, existing_sha: str = None):
    """
    Uploads bytes to given path in repo. Creates or updates file.
    Returns requests.Response or None on error.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("upload disabled: missing GITHUB_TOKEN or GITHUB_REPO")
        return None

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path_in_repo}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode()
    }
    if existing_sha:
        payload["sha"] = existing_sha

    try:
        r = requests.put(api_url, headers=_headers(), json=payload, timeout=20)
        if r.status_code not in (200, 201):
            print("upload_bytes_to_github: GitHub returned", r.status_code, r.text[:400])
        return r
    except Exception as e:
        print("upload_bytes_to_github: request failed:", e)
        return None

def safe_startup_sync():
    """
    Implements the safe A behavior:
    - Fetch remote content and its commit date
    - If remote newer than local -> replace local with remote
    - If local newer than remote -> create remote backup (remote content pushed to GITHUB_DB_PATH.bak.TIMESTAMP),
      then upload local content to the primary path
    - If no remote exists -> do not overwrite anything; upload local as initial file (if exists)
    """
    print("Starting safe startup sync with GitHub...")
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("GitHub sync disabled: GITHUB_TOKEN or GITHUB_REPO not set.")
        return

    status, remote_content, remote_sha, remote_commit_date = get_remote_file_info()
    local_exists = Path(DB_PATH).exists()

    # helper timestamps
    def iso_to_epoch(iso_str):
        try:
            # Example: "2023-08-01T12:34:56Z"
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None

    remote_epoch = iso_to_epoch(remote_commit_date) if remote_commit_date else None
    local_epoch = Path(DB_PATH).stat().st_mtime if local_exists else None

    # Case: remote exists
    if status == 200 and remote_content is not None:
        print("Remote DB found in GitHub (sha:", remote_sha, ").")
        # if local doesn't exist -> write remote to local
        if not local_exists:
            try:
                with open(DB_PATH, "wb") as f:
                    f.write(remote_content)
                print("Local DB created from GitHub remote.")
            except Exception as e:
                print("Failed to write local DB from remote:", e)
            return

        # both exist: compare "newer"
        # prefer commit date from GitHub if available; otherwise compare content hash
        if remote_epoch and local_epoch:
            if remote_epoch > local_epoch + 1:  # remote is newer (add small tolerance)
                try:
                    with open(DB_PATH, "wb") as f:
                        f.write(remote_content)
                    print("Local DB replaced by newer remote DB from GitHub.")
                except Exception as e:
                    print("Failed to overwrite local DB with remote:", e)
                return
            elif local_epoch > remote_epoch + 1:
                # local is newer -> create remote backup then upload local to remote main path
                timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
                backup_path = f"{GITHUB_DB_PATH}.bak.{timestamp}"
                try:
                    # push remote content as backup file
                    if remote_content:
                        upload_bytes_to_github(backup_path, remote_content, f"backup remote DB before startup overwrite {timestamp}")
                        print("Remote DB backed up to", backup_path)
                except Exception as e:
                    print("Failed to create remote backup:", e)
                # upload local
                try:
                    with open(DB_PATH, "rb") as f:
                        local_bytes = f.read()
                    r = upload_bytes_to_github(GITHUB_DB_PATH, local_bytes, f"startup sync: upload local DB {timestamp}", existing_sha=remote_sha)
                    if r is not None and r.status_code in (200,201):
                        print("Local DB uploaded to GitHub main path during startup.")
                    else:
                        print("Local DB upload during startup returned", getattr(r, "status_code", None))
                except Exception as e:
                    print("Failed to upload local DB to GitHub:", e)
                return
            else:
                # timestamps roughly equal, compare content hash to be safe
                with open(DB_PATH, "rb") as f:
                    local_bytes = f.read()
                if _sha256_of_bytes(local_bytes) != _sha256_of_bytes(remote_content):
                    # conflict — back up remote then upload local
                    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
                    backup_path = f"{GITHUB_DB_PATH}.bak.{timestamp}"
                    try:
                        upload_bytes_to_github(backup_path, remote_content, f"backup remote DB before startup conflict {timestamp}")
                        print("Remote DB backed up to", backup_path)
                    except Exception as e:
                        print("Failed to backup remote in conflict:", e)
                    try:
                        r = upload_bytes_to_github(GITHUB_DB_PATH, local_bytes, f"startup sync: upload local DB (conflict) {timestamp}", existing_sha=remote_sha)
                        print("Conflict upload result:", getattr(r, "status_code", None))
                    except Exception as e:
                        print("Failed to upload local DB in conflict:", e)
                else:
                    print("Local and remote DB content identical; nothing to do.")
                return
        else:
            # missing commit dates — fall back to hash comparison
            with open(DB_PATH, "rb") as f:
                local_bytes = f.read()
            if _sha256_of_bytes(local_bytes) == _sha256_of_bytes(remote_content):
                print("Local and remote DB identical (by hash).")
                return
            else:
                # safer approach: backup remote, and upload whichever is newer by mtime if available
                if local_epoch and (not remote_epoch or local_epoch >= (remote_epoch or 0)):
                    # local seems newer — back up remote then upload local
                    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
                    backup_path = f"{GITHUB_DB_PATH}.bak.{timestamp}"
                    try:
                        upload_bytes_to_github(backup_path, remote_content, f"backup remote DB before startup overwrite {timestamp}")
                        print("Remote DB backed up to", backup_path)
                    except Exception as e:
                        print("Failed to create remote backup (no commit date):", e)
                    try:
                        r = upload_bytes_to_github(GITHUB_DB_PATH, local_bytes, f"startup sync: upload local DB {timestamp}", existing_sha=remote_sha)
                        print("Local DB uploaded (no commit date). status:", getattr(r, "status_code", None))
                    except Exception as e:
                        print("Failed to upload local DB (no commit date):", e)
                else:
                    # remote considered newer — overwrite local
                    try:
                        with open(DB_PATH, "wb") as f:
                            f.write(remote_content)
                        print("Local DB replaced by remote (no commit date available).")
                    except Exception as e:
                        print("Failed to write local DB from remote (no commit date):", e)
                return
    else:
        # No remote file found or error reading remote
        print("No remote DB present or fetch failed (status {}).".format(status))
        # If local exists, upload it as initial DB (safe: make initial commit)
        if local_exists:
            try:
                with open(DB_PATH, "rb") as f:
                    local_bytes = f.read()
                timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
                r = upload_bytes_to_github(GITHUB_DB_PATH, local_bytes, f"startup sync: upload local DB initial {timestamp}")
                if r is not None and r.status_code in (200,201):
                    print("Local DB uploaded to GitHub as initial DB.")
                else:
                    print("Initial upload returned", getattr(r, "status_code", None))
            except Exception as e:
                print("Failed initial upload of local DB:", e)
        else:
            print("No local DB either — nothing to sync on startup.")

def download_db_from_github():
    """
    Simpler helper: download remote content to local if remote exists and local missing.
    Kept for compatibility but safe_startup_sync is preferred.
    """
    status, remote_content, remote_sha, remote_commit_date = get_remote_file_info()
    if status == 200 and remote_content:
        try:
            with open(DB_PATH, "wb") as f:
                f.write(remote_content)
            print("download_db_from_github: local DB written from GitHub.")
        except Exception as e:
            print("download_db_from_github: write failed:", e)
    else:
        print("download_db_from_github: remote not present or fetch failed.")

def upload_db_to_github():
    """
    Upload current DB_PATH to GitHub main path. Attempt to include current remote sha if available
    to update instead of creating duplicates.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("upload_db_to_github: disabled (missing token/repo)")
        return
    if not Path(DB_PATH).exists():
        print("upload_db_to_github: no local DB to upload")
        return
    try:
        with open(DB_PATH, "rb") as f:
            local_bytes = f.read()
    except Exception as e:
        print("upload_db_to_github: failed to read local DB:", e)
        return

    # try to fetch current remote sha
    try:
        r = requests.get(GITHUB_API_CONTENTS, headers=_headers(), timeout=15)
        remote_sha = r.json().get("sha") if r.status_code == 200 else None
    except Exception:
        remote_sha = None

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    r2 = upload_bytes_to_github(GITHUB_DB_PATH, local_bytes, f"update database {timestamp}", existing_sha=remote_sha)
    if r2 is not None and r2.status_code in (200,201):
        print("upload_db_to_github: success", r2.status_code)
    else:
        print("upload_db_to_github: failed or returned", getattr(r2, "status_code", None))

# ----------------------- ADMIN DB HELPERS -----------------------
def init_admin():
    conn = sqlite3.connect('app.db')
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY,
            password TEXT NOT NULL
        )
    """)
    # insert default password 1q2w3e if not exists
    c.execute("SELECT * FROM admin")
    if not c.fetchone():
        c.execute("INSERT INTO admin (password) VALUES (?)", ("1q2w3e",))
    conn.commit()
    conn.close()

def get_admin_password_from_db():
    try:
        conn = sqlite3.connect('app.db')
        c = conn.cursor()
        c.execute("SELECT password FROM admin LIMIT 1")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def is_admin_key(k):
    # checks DB directly
    try:
        conn = sqlite3.connect('app.db')
        c = conn.cursor()
        c.execute("SELECT password FROM admin LIMIT 1")
        row = c.fetchone()
        conn.close()
        if not row:
            return False
        return k == row[0]
    except Exception:
        return False

# create admin table and ensure password exists
init_admin()
# If ADMIN_KEY env var is the default or not set, prefer the DB-stored admin password.
_db_pw = get_admin_password_from_db()
if (not os.getenv('FAVHOME_ADMIN_KEY')) or (ADMIN_KEY == 'change_me'):
    if _db_pw:
        ADMIN_KEY = _db_pw

# ----------------------- HELPERS -----------------------
def normalize_location(s):
    return (s or '').strip().lower()

def is_night_time(tstr):
    try:
        if not tstr: return False
        s = tstr.lower().replace(' ', '')
        if 'pm' in s:
            h = int(s.split('pm')[0].split(':')[0]) % 12 + 12
            return h >= 21 or h < 6
        if 'am' in s:
            h = int(s.split('am')[0].split(':')[0]) % 24
            return h < 6
        if ':' in s:
            h = int(s.split(':')[0])
            return h >= 21 or h < 6
        if s.isdigit():
            h = int(s) % 24
            return h >= 21 or h < 6
    except:
        pass
    return False

def compute_fee(pickup, drop, items, preferred_time):
    p = normalize_location(pickup)
    d = normalize_location(drop)
    it = (items or '').lower()
    if ('ebenezer' in p and 'ebenezer' in d) or ('matangi' in p and 'matangi' in d):
        base = 79
    elif 'juja' in p or 'juja' in d or 'jk' in p or 'jkuat' in p:
        base = 99
    else:
        base = 150
    extras = []
    if any(k in it for k in ['supermarket','shop','market','grocery','groceries']):
        extras.append('supermarket_pickup')
        base += 20
    if any(k in it for k in ['water','jerry','sack','sacks','big','heavy']):
        extras.append('heavy_item')
        base += 40
    if is_night_time(preferred_time):
        extras.append('night')
        base += 30
    return base, extras

def require_admin(req):
    """
    Accept admin key via:
     - X-ADMIN-KEY header
     - ?admin_key= in query string
     - JSON body { "admin_key": "..." }
    The provided key is accepted if it matches either the ADMIN_KEY env value OR the password stored in app.db.
    """
    key = None
    try:
        key = req.headers.get('X-ADMIN-KEY') or req.args.get('admin_key')
        if not key and req.is_json:
            j = req.get_json(silent=True) or {}
            key = j.get('admin_key') or j.get('adminKey') or j.get('admin')
    except Exception:
        key = None

    if not key:
        return False

    # match against configured ADMIN_KEY (env or overridden by DB) OR the DB password
    if key == ADMIN_KEY:
        return True
    if is_admin_key(key):
        return True
    return False

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

# ----------------------- STATIC INDEX -----------------------
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/favmarket.html')
def favmarket():
    return send_from_directory('.', 'favmarket.html')

# ----------------------- MANIFEST -----------------------
@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "FavHome Deliveries",
        "short_name": "FavHome",
        "start_url": ".",
        "display": "standalone",
        "background_color": "#f7fbff",
        "theme_color": "#0b76ef",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })

# ----------------------- UPLOAD IMAGE (for market) -----------------------
@app.route('/upload_image', methods=['POST'])
def upload_image():
    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({"ok": False, "error": "empty filename"}), 400
    if not allowed_file(f.filename):
        return jsonify({"ok": False, "error": "filetype not allowed"}), 400
    filename = secure_filename(f"{int(datetime.utcnow().timestamp())}_{f.filename}")
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    f.save(save_path)
    url = f"/{UPLOAD_FOLDER}/{filename}"
    return jsonify({"ok": True, "url": url})

# ----------------------- MARKET API -----------------------
@app.route('/market', methods=['POST'])
def market_post():
    try:
        data = request.get_json(force=True)
        seller_name = data.get('sellerName', '').strip()
        phone = data.get('phone', '').strip()
        title = data.get('title', '').strip()
        description = data.get('description', '').strip()
        price = int(data.get('price') or 0)
        payment = data.get('payment', '').strip()
        image = data.get('image', '').strip()
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO market(seller_name,phone,title,description,price,payment,image,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (seller_name, phone, title, description, price, payment, image, created_at)
            )
            mid = c.lastrowid
            conn.commit()

        # upload DB to GitHub after mutation
        upload_db_to_github()
        return jsonify({"status": "ok", "market_id": mid})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def fetch_market_rows(public=False):
    query = "SELECT id,seller_name,phone,title,description,price,payment,image,status,created_at FROM market"
    if public:
        query += " WHERE status='available'"
    query += " ORDER BY id DESC"
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(query)
        rows = c.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "seller_name": r[1] or '',
            "phone": r[2] or '',
            "title": r[3] or '',
            "description": r[4] or '',
            "price": r[5] or 0,
            "payment": r[6] or '',
            "image": r[7] or '',
            "status": r[8] or '',
            "created_at": r[9] or ''
        })
    return out

@app.route('/api/market')
def api_market():
    return jsonify(fetch_market_rows())

@app.route('/api/market/public')
def api_market_public():
    return jsonify(fetch_market_rows(public=True))

# ----------------------- MARKET EDIT/DELETE -----------------------
@app.route('/market/<int:mid>', methods=['PUT'])
def market_edit(mid):
    try:
        data = request.get_json(force=True)
        title = data.get('title', '').strip()
        description = data.get('description', '').strip()
        price = int(data.get('price') or 0)
        payment = data.get('payment', '').strip()
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE market SET title=?, description=?, price=?, payment=? WHERE id=?",
                (title, description, price, payment, mid)
            )
            conn.commit()

        upload_db_to_github()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/market/<int:mid>', methods=['DELETE'])
def market_delete(mid):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM market WHERE id=?", (mid,))
            conn.commit()

        upload_db_to_github()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ----------------------- ORDER API -----------------------
@app.route('/order', methods=['POST'])
def order():
    try:
        data = request.get_json(force=True)
        name = data.get('name','').strip()
        phone = data.get('phone','').strip()
        pickup = data.get('pickup','').strip()
        drop_loc = data.get('drop','').strip()
        items = data.get('items','').strip()
        preferred_time = data.get('time','').strip()
        payment = data.get('payment','').strip()

        fee, extras = compute_fee(pickup, drop_loc, items, preferred_time)
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO orders(name,phone,pickup,drop_loc,items,preferred_time,payment,fee,extras,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (name,phone,pickup,drop_loc,items,preferred_time,payment,fee,','.join(extras),created_at))
        conn.commit()
        oid = c.lastrowid
        conn.close()

        # upload DB to GitHub after mutation
        upload_db_to_github()
        return jsonify({"status":"ok","order_id":oid,"fee":fee})

    except Exception as e:
        return jsonify({"status":"error","message":str(e)}),500

# ----------------------- ADMIN API -----------------------
@app.route('/api/orders')
def api_orders():
    # admin-only listing (orders + market)
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id,name,phone,pickup,drop_loc,items,preferred_time,payment,payment_status,fee,extras,status,created_at FROM orders ORDER BY id DESC"
    )
    order_rows = c.fetchall()
    c.execute(
        "SELECT id,seller_name,phone,title,description,price,payment,image,status,created_at FROM market ORDER BY id DESC"
    )
    market_rows = c.fetchall()
    conn.close()

    orders = []
    for r in order_rows:
        orders.append({
            'id': r[0],
            'name': r[1] or '',
            'phone': r[2] or '',
            'pickup': r[3] or '',
            'drop': r[4] or '',
            'items': r[5] or '',
            'preferred_time': r[6] or '',
            'payment': r[7] or '',
            'payment_status': r[8] or '',
            'fee': r[9] if r[9] is not None else 0,
            'extras': r[10] or '',
            'status': r[11] or '',
            'created_at': r[12] or ''
        })

    market = []
    for r in market_rows:
        market.append({
            'id': r[0],
            'seller_name': r[1] or '',
            'phone': r[2] or '',
            'title': r[3] or '',
            'description': r[4] or '',
            'price': r[5] if r[5] is not None else 0,
            'payment': r[6] or '',
            'image': r[7] or '',
            'status': r[8] or '',
            'created_at': r[9] or ''
        })

    return jsonify({'orders': orders, 'market': market})

# ----------------------- HIDDEN ADMIN UI -----------------------
@app.route('/1q2w3e')
def admin_ui():
    if not require_admin(request):
        return abort(401)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id,name,phone,pickup,drop_loc,items,preferred_time,payment,payment_status,fee,extras,status,created_at FROM orders ORDER BY id DESC")
    order_rows = c.fetchall()
    c.execute("SELECT id,seller_name,phone,title,description,price,payment,image,status,created_at FROM market ORDER BY id DESC")
    market_rows = c.fetchall()
    conn.close()

    # build rows with safe escaping and None -> ''
    order_trs = ''
    for r in order_rows:
        oid = r[0]
        name = html.escape(r[1] or '')
        phone = html.escape(r[2] or '')
        pickup = html.escape(r[3] or '')
        drop_loc = html.escape(r[4] or '')
        items = html.escape(r[5] or '')
        ptime = html.escape(r[6] or '')
        payment = html.escape(r[7] or '')
        payment_status = html.escape(r[8] or '')
        fee = r[9] if r[9] is not None else 0
        extras = html.escape(r[10] or '')
        status = html.escape(r[11] or '')
        created_at = html.escape(r[12] or '')

        order_trs += (
            "<tr>"
            f"<td>{oid}</td>"
            f"<td>{name}</td>"
            f"<td>{phone}</td>"
            f"<td contenteditable='true' onBlur=\"updateField({oid}, 'pickup', this.innerText)\">{pickup}</td>"
            f"<td contenteditable='true' onBlur=\"updateField({oid}, 'drop_loc', this.innerText)\">{drop_loc}</td>"
            f"<td>{items}</td>"
            f"<td>{ptime}</td>"
            f"<td>{payment}</td>"
            f"<td>{payment_status}</td>"
            f"<td>{fee}</td>"
            f"<td>{extras}</td>"
            f"<td>{status}</td>"
            "<td>"
            f"<button onclick=\"markDelivered({oid})\">Delivered</button>"
            f"<button onclick=\"approvePayment({oid})\">Approve</button>"
            f"<button onclick=\"disapprovePayment({oid})\">Disapprove</button>"
            f"<button onclick=\"deleteOrder({oid})\" style=\"background:#e74c3c\">Delete</button>"
            "</td>"
            "</tr>"
        )

    market_trs = ''
    for r in market_rows:
        mid = r[0]
        seller_name = html.escape(r[1] or '')
        seller_phone = html.escape(r[2] or '')
        title = html.escape(r[3] or '')
        description = html.escape(r[4] or '')
        price = r[5] if r[5] is not None else 0
        payment = html.escape(r[6] or '')
        image = html.escape(r[7] or '')
        status = html.escape(r[8] or '')
        created_at = html.escape(r[9] or '')

        market_trs += (
            "<tr>"
            f"<td>{mid}</td>"
            f"<td>{seller_name}</td>"
            f"<td>{seller_phone}</td>"
            f"<td contenteditable='true' onBlur=\"updateFieldMarket({mid}, 'title', this.innerText)\">{title}</td>"
            f"<td contenteditable='true' onBlur=\"updateFieldMarket({mid}, 'description', this.innerText)\">{description}</td>"
            f"<td contenteditable='true' onBlur=\"updateFieldMarket({mid}, 'price', this.innerText)\">{price}</td>"
            f"<td>{payment}</td>"
            f"<td>{image}</td>"
            f"<td>{status}</td>"
            f"<td>{created_at}</td>"
            "<td>"
            f"<button onclick=\"markSold({mid})\">Sold</button>"
            f"<button onclick=\"deleteMarket({mid})\" style=\"background:#e74c3c\">Delete</button>"
            "</td>"
            "</tr>"
        )

    # assemble html by concatenation to avoid f-string brace issues
    html_page = (
        "<!doctype html><html><head><meta charset='utf-8'><title>FavHome Hidden Admin</title>"
        "<style>table{width:100%;border-collapse:collapse;margin-bottom:30px;}th,td{border:1px solid #ddd;padding:6px;}th{background:#f3f3f3;}button{padding:4px 8px;border-radius:6px;background:#27ae60;color:white;border:none;cursor:pointer;}button:hover{background:#2ecc71;}h2{margin-top:30px;}pre{white-space:pre-wrap;word-break:break-word;}</style>"
        "</head><body>"
        "<h2>FavHome Orders</h2>"
        "<table><thead>"
        "<tr><th>ID</th><th>Name</th><th>Phone</th><th>Pickup</th><th>Drop</th><th>Items</th><th>Time</th><th>Payment</th><th>Payment Status</th><th>Fee</th><th>Extras</th><th>Status</th><th>Action</th></tr>"
        "</thead><tbody>"
        + order_trs +
        "</tbody></table>"

        "<h2>FavMarket Listings</h2>"
        "<table><thead>"
        "<tr><th>ID</th><th>Seller</th><th>Phone</th><th>Title</th><th>Description</th><th>Price</th><th>Payment</th><th>Image</th><th>Status</th><th>Created</th><th>Action</th></tr>"
        "</thead><tbody>"
        + market_trs +
        "</tbody></table>"

        # JS (ADMIN_KEY inserted)
        "<script>const ADMIN_KEY = '" + ADMIN_KEY + "';\n"
        "function markDelivered(id){ fetch('/1q2w3e/mark/' + id + '?admin_key=' + ADMIN_KEY, {method:'POST'}).then(()=>location.reload()); }\n"
        "function markSold(id){ fetch('/1q2w3e/market/mark/' + id + '?admin_key=' + ADMIN_KEY, {method:'POST'}).then(()=>location.reload()); }\n"
        "function approvePayment(id){ fetch('/1q2w3e/payment/' + id + '/approve?admin_key=' + ADMIN_KEY, {method:'POST'}).then(()=>location.reload()); }\n"
        "function disapprovePayment(id){ fetch('/1q2w3e/payment/' + id + '/disapprove?admin_key=' + ADMIN_KEY, {method:'POST'}).then(()=>location.reload()); }\n"
        "function deleteOrder(id){ fetch('/1q2w3e/order/delete/' + id + '?admin_key=' + ADMIN_KEY, {method:'POST'}).then(()=>location.reload()); }\n"
        "function deleteMarket(id){ fetch('/1q2w3e/market/delete/' + id + '?admin_key=' + ADMIN_KEY, {method:'POST'}).then(()=>location.reload()); }\n"
        "function updateField(id, field, value){ fetch('/1q2w3e/order/edit/' + id, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({field: field, value: value, admin_key: ADMIN_KEY}) }); }\n"
        "function updateFieldMarket(id, field, value){ fetch('/1q2w3e/market/edit/' + id, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({field: field, value: value, admin_key: ADMIN_KEY}) }); }\n"
        "</script></body></html>"
    )

    return html_page

# ----------------------- ADMIN ACTIONS -----------------------
@app.route('/1q2w3e/mark/<int:oid>', methods=['POST'])
def mark(oid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE orders SET status='delivered' WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

@app.route('/1q2w3e/market/mark/<int:mid>', methods=['POST'])
def market_mark(mid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE market SET status='sold' WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

@app.route('/1q2w3e/payment/<int:oid>/approve', methods=['POST'])
def approve_payment(oid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE orders SET payment_status='approved' WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

@app.route('/1q2w3e/payment/<int:oid>/disapprove', methods=['POST'])
def disapprove_payment(oid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE orders SET payment_status='disapproved' WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

@app.route('/1q2w3e/order/delete/<int:oid>', methods=['POST'])
def delete_order(oid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM orders WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

@app.route('/1q2w3e/market/delete/<int:mid>', methods=['POST'])
def delete_market(mid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM market WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

@app.route('/1q2w3e/order/edit/<int:oid>', methods=['POST'])
def edit_order(oid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    data = request.json
    field = data.get('field')
    value = data.get('value')
    if field not in ['pickup','drop_loc']:
        return jsonify({'error':'invalid field'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE orders SET {field}=? WHERE id=?", (value, oid))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

@app.route('/1q2w3e/market/edit/<int:mid>', methods=['POST'])
def edit_market(mid):
    if not require_admin(request):
        return jsonify({'error':'unauthorized'}), 401
    data = request.json
    field = data.get('field')
    value = data.get('value')
    if field not in ['title','description','price']:
        return jsonify({'error':'invalid field'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE market SET {field}=? WHERE id=?", (value, mid))
    conn.commit()
    conn.close()
    upload_db_to_github()
    return jsonify({'status':'ok'})

# ----------------------- PUBLIC FEEDS -----------------------
@app.route('/api/orders/public')
def public_orders():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id,name,phone,pickup,drop_loc,items,preferred_time,payment,fee,extras,status,created_at FROM orders ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0] or 0,
            "name": r[1] or '',
            "phone": r[2] or '',
            "pickup": r[3] or '',
            "drop": r[4] or '',
            "items": r[5] or '',
            "preferred_time": r[6] or '',
            "payment": r[7] or '',
            "fee": r[8] if r[8] is not None else 0,
            "extras": r[9] or '',
            "status": r[10] or '',
            "created_at": r[11] or ''
        })
    return jsonify(out)

# ----------------------- ROBOTS & SITEMAP -----------------------
@app.route('/robots.txt')
def robots():
    txt = "User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n# Hidden admin page not listed"
    return app.response_class(txt, mimetype='text/plain')

@app.route('/sitemap.xml')
def sitemap():
    base = request.host_url.rstrip('/')
    urls = ['/', '/order', '/favmarket.html']
    body = ''.join([f"<url><loc>{base}{u}</loc></url>\n" for u in urls])
    xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n{body}</urlset>"
    return app.response_class(xml, mimetype='application/xml')

# ----------------------- STATIC PROXY (serves uploads and other static files) -----------------------
@app.route('/<path:p>')
def static_proxy(p):
    # only serve from known folders
    allowed_folders = ['uploads']
    if any(p.startswith(f"{f}/") for f in allowed_folders):
        return send_from_directory('.', p)
    return abort(404)

# ----------------------- KEEP-ALIVE PING -----------------------
@app.route('/ping')
def ping():
    return jsonify({"alive": True, "ts": datetime.now().timestamp()})

def init_main_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Orders table
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            pickup TEXT,
            drop_loc TEXT,
            items TEXT,
            preferred_time TEXT,
            payment TEXT,
            payment_status TEXT DEFAULT '',
            fee INTEGER DEFAULT 0,
            extras TEXT,
            status TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    # Market table
    c.execute("""
        CREATE TABLE IF NOT EXISTS market (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_name TEXT,
            phone TEXT,
            title TEXT,
            description TEXT,
            price INTEGER DEFAULT 0,
            payment TEXT,
            image TEXT,
            status TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

# Initialize DBs
# Note: init_admin() was already called earlier to ensure admin exists.
init_main_db()

# ----------------------- STARTUP SYNC & RUN -----------------------
# Perform safe startup sync (this may upload local DB only after creating a remote backup if needed)
safe_startup_sync()

if __name__ == '__main__':
    print("FavHome Deliveries running...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
