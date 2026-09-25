from flask import Blueprint, jsonify, request, session
from datetime import datetime
import sqlite3

bp = Blueprint("v3", __name__, url_prefix="/api/v3")

V3_SCHEMA = """
CREATE TABLE IF NOT EXISTS v3_price_rules(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, rule_type TEXT NOT NULL DEFAULT 'percent',
 value REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, starts_at TEXT, ends_at TEXT
);
CREATE TABLE IF NOT EXISTS v3_item_kits(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sku TEXT UNIQUE, price REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS v3_item_kit_lines(
 id INTEGER PRIMARY KEY AUTOINCREMENT, kit_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity REAL NOT NULL DEFAULT 1,
 FOREIGN KEY(kit_id) REFERENCES v3_item_kits(id), FOREIGN KEY(product_id) REFERENCES products(id)
);
CREATE TABLE IF NOT EXISTS v3_receivings(
 id INTEGER PRIMARY KEY AUTOINCREMENT, receiving_no TEXT UNIQUE NOT NULL, supplier_id INTEGER, store_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'received', subtotal REAL NOT NULL DEFAULT 0, created_by INTEGER, created_at TEXT NOT NULL,
 FOREIGN KEY(supplier_id) REFERENCES suppliers(id), FOREIGN KEY(store_id) REFERENCES stores(id), FOREIGN KEY(created_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS v3_receiving_lines(
 id INTEGER PRIMARY KEY AUTOINCREMENT, receiving_id INTEGER NOT NULL, product_id INTEGER NOT NULL, lot_number TEXT,
 expiry_date TEXT, quantity REAL NOT NULL, unit_cost REAL NOT NULL DEFAULT 0,
 FOREIGN KEY(receiving_id) REFERENCES v3_receivings(id), FOREIGN KEY(product_id) REFERENCES products(id)
);
CREATE TABLE IF NOT EXISTS v3_transfers(
 id INTEGER PRIMARY KEY AUTOINCREMENT, transfer_no TEXT UNIQUE NOT NULL, from_store_id INTEGER NOT NULL, to_store_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft', notes TEXT, created_by INTEGER, created_at TEXT NOT NULL, received_at TEXT,
 FOREIGN KEY(from_store_id) REFERENCES stores(id), FOREIGN KEY(to_store_id) REFERENCES stores(id)
);
CREATE TABLE IF NOT EXISTS v3_transfer_lines(
 id INTEGER PRIMARY KEY AUTOINCREMENT, transfer_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity REAL NOT NULL,
 FOREIGN KEY(transfer_id) REFERENCES v3_transfers(id), FOREIGN KEY(product_id) REFERENCES products(id)
);
CREATE TABLE IF NOT EXISTS v3_expenses(
 id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER NOT NULL, expense_date TEXT NOT NULL, category TEXT, description TEXT,
 amount REAL NOT NULL DEFAULT 0, payment_method TEXT, created_by INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_time_clock(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, store_id INTEGER, clock_in TEXT NOT NULL, clock_out TEXT,
 notes TEXT, FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS v3_deliveries(
 id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_no TEXT UNIQUE NOT NULL, sale_id INTEGER, customer_id INTEGER, store_id INTEGER,
 address TEXT, driver_name TEXT, status TEXT NOT NULL DEFAULT 'pending', scheduled_at TEXT, delivered_at TEXT, notes TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_gift_cards(
 id INTEGER PRIMARY KEY AUTOINCREMENT, card_no TEXT UNIQUE NOT NULL, customer_id INTEGER, balance REAL NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_store_config(
 id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER NOT NULL, config_key TEXT NOT NULL, config_value TEXT,
 UNIQUE(store_id, config_key), FOREIGN KEY(store_id) REFERENCES stores(id)
);
"""

def now():
    return datetime.utcnow().isoformat()

def _db(db_fn):
    return db_fn()

def register_v3(app, db_fn, audit_fn):
    conn=_db(db_fn)
    conn.executescript(V3_SCHEMA)
    conn.commit(); conn.close()

    def rows(sql, params=()):
        c=_db(db_fn); r=[dict(x) for x in c.execute(sql,params).fetchall()]; c.close(); return r
    def one(sql, params=()):
        c=_db(db_fn); r=c.execute(sql,params).fetchone(); c.close(); return dict(r) if r else None

    @bp.before_request
    def require_login():
        if 'user_id' not in session:
            return jsonify(error='Authentication required'),401

    @bp.get('/customers')
    def customers():
        return jsonify(rows("SELECT * FROM customers ORDER BY full_name LIMIT 500"))

    @bp.post('/customers')
    def customer_add():
        d=request.get_json(force=True); c=_db(db_fn)
        cur=c.execute("INSERT INTO customers(customer_no,full_name,phone,email,dob,notes) VALUES(?,?,?,?,?,?)",
                      (d.get('customer_no'),d['full_name'],d.get('phone'),d.get('email'),d.get('dob'),d.get('notes')))
        c.commit(); c.close(); audit_fn('create','customer',str(cur.lastrowid),d.get('full_name')); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/items')
    def items():
        return jsonify(rows("""SELECT p.*, COALESCE(SUM(b.quantity),0) stock FROM products p
          LEFT JOIN batches b ON b.product_id=p.id GROUP BY p.id ORDER BY p.name LIMIT 1000"""))

    @bp.post('/items')
    def item_add():
        d=request.get_json(force=True); c=_db(db_fn)
        cur=c.execute("""INSERT INTO products(sku,barcode,name,strength,dosage_form,category,prescription_required,controlled,cost,retail_price,reorder_level)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(d['sku'],d.get('barcode'),d['name'],d.get('strength'),d.get('dosage_form'),d.get('category'),
          int(bool(d.get('prescription_required'))),int(bool(d.get('controlled'))),float(d.get('cost') or 0),float(d.get('retail_price') or 0),float(d.get('reorder_level') or 0)))
        c.commit(); c.close(); audit_fn('create','product',str(cur.lastrowid),d.get('name')); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/suppliers')
    def supplier_list():
        return jsonify(rows("SELECT * FROM suppliers ORDER BY name LIMIT 500"))

    @bp.post('/suppliers')
    def supplier_add():
        d=request.get_json(force=True); c=_db(db_fn)
        cur=c.execute("INSERT INTO suppliers(supplier_code,name,phone,email,terms) VALUES(?,?,?,?,?)",
                      (d.get('supplier_code'),d['name'],d.get('phone'),d.get('email'),d.get('terms')))
        c.commit(); c.close(); audit_fn('create','supplier',str(cur.lastrowid),d.get('name')); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/receiving')
    def receiving_list():
        return jsonify(rows("""SELECT r.*, s.name supplier, st.name store FROM v3_receivings r
          LEFT JOIN suppliers s ON s.id=r.supplier_id JOIN stores st ON st.id=r.store_id ORDER BY r.id DESC LIMIT 200"""))

    @bp.post('/receiving')
    def receiving_add():
        d=request.get_json(force=True); c=_db(db_fn); no=d.get('receiving_no') or 'REC-'+datetime.now().strftime('%y%m%d%H%M%S')
        cur=c.execute("INSERT INTO v3_receivings(receiving_no,supplier_id,store_id,status,subtotal,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                      (no,d.get('supplier_id'),d['store_id'],'received',0,session['user_id'],now()))
        rid=cur.lastrowid; subtotal=0
        for line in d.get('lines',[]):
            qty=float(line.get('quantity') or 0); cost=float(line.get('unit_cost') or 0); subtotal+=qty*cost
            c.execute("INSERT INTO v3_receiving_lines(receiving_id,product_id,lot_number,expiry_date,quantity,unit_cost) VALUES(?,?,?,?,?,?)",
                      (rid,line['product_id'],line.get('lot_number') or 'NOLOT',line.get('expiry_date'),qty,cost))
            batch=c.execute("SELECT id FROM batches WHERE product_id=? AND store_id=? AND lot_number=?",(line['product_id'],d['store_id'],line.get('lot_number') or 'NOLOT')).fetchone()
            if batch: c.execute("UPDATE batches SET quantity=quantity+?, unit_cost=?, expiry_date=COALESCE(?,expiry_date) WHERE id=?",(qty,cost,line.get('expiry_date'),batch['id']))
            else: c.execute("INSERT INTO batches(product_id,store_id,lot_number,expiry_date,quantity,unit_cost,received_at) VALUES(?,?,?,?,?,?,?)",
                            (line['product_id'],d['store_id'],line.get('lot_number') or 'NOLOT',line.get('expiry_date'),qty,cost,now()))
        c.execute("UPDATE v3_receivings SET subtotal=? WHERE id=?",(subtotal,rid)); c.commit(); c.close()
        audit_fn('receive_stock','receiving',str(rid),no); return jsonify(ok=True,id=rid,receiving_no=no,subtotal=subtotal)

    @bp.get('/sales-history')
    def sales_history():
        return jsonify(rows("""SELECT s.*, st.name store, u.full_name cashier, c.full_name customer FROM sales s
          JOIN stores st ON st.id=s.store_id JOIN users u ON u.id=s.user_id LEFT JOIN customers c ON c.id=s.customer_id
          ORDER BY s.id DESC LIMIT 500"""))

    @bp.get('/reports/summary')
    def reports_summary():
        c=_db(db_fn)
        out={
          'sales_today': c.execute("SELECT COALESCE(SUM(total),0) v FROM sales WHERE date(created_at)=date('now')").fetchone()['v'],
          'sales_month': c.execute("SELECT COALESCE(SUM(total),0) v FROM sales WHERE strftime('%Y-%m',created_at)=strftime('%Y-%m','now')").fetchone()['v'],
          'expenses_month': c.execute("SELECT COALESCE(SUM(amount),0) v FROM v3_expenses WHERE strftime('%Y-%m',expense_date)=strftime('%Y-%m','now')").fetchone()['v'],
          'inventory_value': c.execute("SELECT COALESCE(SUM(b.quantity*b.unit_cost),0) v FROM batches b").fetchone()['v'],
          'rx_open': c.execute("SELECT COUNT(*) v FROM prescriptions WHERE status NOT IN ('collected','cancelled')").fetchone()['v'],
          'customers': c.execute("SELECT COUNT(*) v FROM customers").fetchone()['v']
        }; c.close(); return jsonify(out)

    @bp.get('/locations')
    def locations():
        return jsonify(rows("SELECT * FROM stores ORDER BY id"))

    @bp.post('/locations')
    def location_add():
        d=request.get_json(force=True); c=_db(db_fn); cur=c.execute("INSERT INTO stores(code,name,active) VALUES(?,?,1)",(d['code'],d['name'])); c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/transfers')
    def transfer_list():
        return jsonify(rows("""SELECT t.*, a.name from_store, b.name to_store FROM v3_transfers t
          JOIN stores a ON a.id=t.from_store_id JOIN stores b ON b.id=t.to_store_id ORDER BY t.id DESC LIMIT 200"""))

    @bp.post('/transfers')
    def transfer_add():
        d=request.get_json(force=True); c=_db(db_fn); no=d.get('transfer_no') or 'TR-'+datetime.now().strftime('%y%m%d%H%M%S')
        cur=c.execute("INSERT INTO v3_transfers(transfer_no,from_store_id,to_store_id,status,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                      (no,d['from_store_id'],d['to_store_id'],'draft',d.get('notes'),session['user_id'],now()))
        tid=cur.lastrowid
        for line in d.get('lines',[]): c.execute("INSERT INTO v3_transfer_lines(transfer_id,product_id,quantity) VALUES(?,?,?)",(tid,line['product_id'],line['quantity']))
        c.commit(); c.close(); return jsonify(ok=True,id=tid,transfer_no=no)

    @bp.get('/employees')
    def employees():
        return jsonify(rows("""SELECT u.id,u.username,u.full_name,u.role,u.active,u.default_store_id,s.name store
          FROM users u LEFT JOIN stores s ON s.id=u.default_store_id ORDER BY u.full_name"""))

    @bp.post('/employees')
    def employee_add():
        if session.get('role')!='admin': return jsonify(error='Admin only'),403
        from werkzeug.security import generate_password_hash
        d=request.get_json(force=True); c=_db(db_fn)
        cur=c.execute("INSERT INTO users(username,full_name,role,password_hash,default_store_id,active,created_at) VALUES(?,?,?,?,?,1,?)",
                      (d['username'],d['full_name'],d['role'],generate_password_hash(d['password']),d.get('default_store_id'),now()))
        c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/expenses')
    def expenses():
        return jsonify(rows("""SELECT e.*, s.name store FROM v3_expenses e JOIN stores s ON s.id=e.store_id ORDER BY e.expense_date DESC,e.id DESC LIMIT 500"""))

    @bp.post('/expenses')
    def expense_add():
        d=request.get_json(force=True); c=_db(db_fn)
        cur=c.execute("INSERT INTO v3_expenses(store_id,expense_date,category,description,amount,payment_method,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (d['store_id'],d.get('expense_date') or datetime.now().date().isoformat(),d.get('category'),d.get('description'),float(d.get('amount') or 0),d.get('payment_method'),session['user_id'],now()))
        c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/time-clock')
    def time_clock():
        return jsonify(rows("""SELECT t.*,u.full_name,s.name store FROM v3_time_clock t JOIN users u ON u.id=t.user_id LEFT JOIN stores s ON s.id=t.store_id ORDER BY t.id DESC LIMIT 300"""))

    @bp.post('/time-clock/clock-in')
    def clock_in():
        c=_db(db_fn); openrow=c.execute("SELECT id FROM v3_time_clock WHERE user_id=? AND clock_out IS NULL ORDER BY id DESC LIMIT 1",(session['user_id'],)).fetchone()
        if openrow: c.close(); return jsonify(error='Already clocked in'),400
        cur=c.execute("INSERT INTO v3_time_clock(user_id,store_id,clock_in) VALUES(?,?,?)",(session['user_id'],session.get('store_id'),now())); c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid)

    @bp.post('/time-clock/clock-out')
    def clock_out():
        c=_db(db_fn); r=c.execute("SELECT id FROM v3_time_clock WHERE user_id=? AND clock_out IS NULL ORDER BY id DESC LIMIT 1",(session['user_id'],)).fetchone()
        if not r: c.close(); return jsonify(error='No open shift'),400
        c.execute("UPDATE v3_time_clock SET clock_out=? WHERE id=?",(now(),r['id'])); c.commit(); c.close(); return jsonify(ok=True,id=r['id'])

    @bp.get('/price-rules')
    def price_rules():
        return jsonify(rows("SELECT * FROM v3_price_rules ORDER BY id DESC"))

    @bp.post('/price-rules')
    def price_rule_add():
        d=request.get_json(force=True); c=_db(db_fn); cur=c.execute("INSERT INTO v3_price_rules(name,rule_type,value,active,starts_at,ends_at) VALUES(?,?,?,?,?,?)",
          (d['name'],d.get('rule_type','percent'),float(d.get('value') or 0),1,d.get('starts_at'),d.get('ends_at'))); c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/item-kits')
    def kits():
        return jsonify(rows("SELECT * FROM v3_item_kits ORDER BY name"))

    @bp.post('/item-kits')
    def kit_add():
        d=request.get_json(force=True); c=_db(db_fn); cur=c.execute("INSERT INTO v3_item_kits(name,sku,price,active) VALUES(?,?,?,1)",
            (d['name'],d.get('sku'),float(d.get('price') or 0))); c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/deliveries')
    def deliveries():
        return jsonify(rows("""SELECT d.*,c.full_name customer,s.name store FROM v3_deliveries d
          LEFT JOIN customers c ON c.id=d.customer_id LEFT JOIN stores s ON s.id=d.store_id ORDER BY d.id DESC LIMIT 300"""))

    @bp.post('/deliveries')
    def delivery_add():
        d=request.get_json(force=True); c=_db(db_fn); no=d.get('delivery_no') or 'DEL-'+datetime.now().strftime('%y%m%d%H%M%S')
        cur=c.execute("INSERT INTO v3_deliveries(delivery_no,sale_id,customer_id,store_id,address,driver_name,status,scheduled_at,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (no,d.get('sale_id'),d.get('customer_id'),d.get('store_id'),d.get('address'),d.get('driver_name'),'pending',d.get('scheduled_at'),d.get('notes'),now()))
        c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid,delivery_no=no)

    @bp.get('/gift-cards')
    def gift_cards():
        return jsonify(rows("SELECT g.*,c.full_name customer FROM v3_gift_cards g LEFT JOIN customers c ON c.id=g.customer_id ORDER BY g.id DESC"))

    @bp.post('/gift-cards')
    def gift_card_add():
        d=request.get_json(force=True); c=_db(db_fn); cur=c.execute("INSERT INTO v3_gift_cards(card_no,customer_id,balance,active,created_at) VALUES(?,?,?,1,?)",
          (d['card_no'],d.get('customer_id'),float(d.get('balance') or 0),now())); c.commit(); c.close(); return jsonify(ok=True,id=cur.lastrowid)

    @bp.get('/store-config')
    def store_config():
        store_id=request.args.get('store_id') or session.get('store_id'); return jsonify(rows("SELECT * FROM v3_store_config WHERE store_id=? ORDER BY config_key",(store_id,)))

    @bp.post('/store-config')
    def store_config_set():
        d=request.get_json(force=True); c=_db(db_fn)
        c.execute("""INSERT INTO v3_store_config(store_id,config_key,config_value) VALUES(?,?,?)
          ON CONFLICT(store_id,config_key) DO UPDATE SET config_value=excluded.config_value""",(d['store_id'],d['config_key'],d.get('config_value')))
        c.commit(); c.close(); return jsonify(ok=True)

    app.register_blueprint(bp)
