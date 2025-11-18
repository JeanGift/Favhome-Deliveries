# full updated app.py
import os
import sqlite3
import html
from flask import Flask, request, jsonify, send_from_directory, abort
from datetime import datetime
from pathlib import Path
from werkzeug.utils import secure_filename

# ----------------------- CONFIG -----------------------
app = Flask(__name__, static_folder='.', template_folder='.')
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_key_here')

DB_PATH = 'orders.db'
ADMIN_KEY = os.getenv('FAVHOME_ADMIN_KEY', 'change_me')
PAYBILL = os.getenv('FAVHOME_PAYBILL', '400200')

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# ----------------------- DB SETUP (single, correct) -----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # create orders with payment_status column included so older DBs lacking it won't fail later
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        phone TEXT,
        pickup TEXT,
        drop_loc TEXT,
        items TEXT,
        preferred_time TEXT,
        payment TEXT,
        fee INTEGER DEFAULT 0,
        extras TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        payment_status TEXT DEFAULT '',
        created_at TEXT
    )''')
    # create market table
    c.execute('''CREATE TABLE IF NOT EXISTS market (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_name TEXT,
        phone TEXT,
        title TEXT,
        description TEXT,
        price INTEGER,
        payment TEXT,
        image TEXT DEFAULT '',
        status TEXT DEFAULT 'available',
        created_at TEXT
    )''')
    conn.commit()
    conn.close()

def update_db_columns():
    # ensure columns exist for older DBs
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE orders ADD COLUMN fee INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE orders ADD COLUMN extras TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

init_db()
update_db_columns()

# ----------------------- HELPERS -----------------------
def normalize_location(s):
    return (s or '').strip().lower()

def is_night_time(tstr):
    try:
        if not tstr:
            return False
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
    key = req.headers.get('X-ADMIN-KEY') or req.args.get('admin_key')
    return key == ADMIN_KEY

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
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/market/<int:mid>', methods=['DELETE'])
def market_delete(mid):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM market WHERE id=?", (mid,))
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

# ----------------------- RUN -----------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)


