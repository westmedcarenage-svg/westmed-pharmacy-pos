from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, csv, io
from datetime import datetime
from functools import wraps
from v3 import register_v3
from v5 import register_v5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = '/data/westmed_v5.db' if os.path.isdir('/data') else os.path.join(BASE_DIR, 'westmed_pos.db')
DB_PATH = os.environ.get('WESTMED_DB_PATH', DEFAULT_DB)
SECRET_KEY = os.environ.get('WESTMED_SECRET_KEY', 'CHANGE-ME-BEFORE-PRODUCTION')

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['WESTMED_DB_PATH'] = DB_PATH

SCHEMA = '''
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','pharmacist','cashier','manager')),
  password_hash TEXT NOT NULL,
  default_store_id INTEGER,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  FOREIGN KEY(default_store_id) REFERENCES stores(id)
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku TEXT UNIQUE NOT NULL,
  barcode TEXT,
  name TEXT NOT NULL,
  strength TEXT,
  dosage_form TEXT,
  category TEXT,
  prescription_required INTEGER NOT NULL DEFAULT 0,
  controlled INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  retail_price REAL NOT NULL DEFAULT 0,
  reorder_level REAL NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  store_id INTEGER NOT NULL,
  lot_number TEXT NOT NULL,
  expiry_date TEXT,
  quantity REAL NOT NULL DEFAULT 0,
  unit_cost REAL NOT NULL DEFAULT 0,
  received_at TEXT,
  UNIQUE(product_id, store_id, lot_number),
  FOREIGN KEY(product_id) REFERENCES products(id),
  FOREIGN KEY(store_id) REFERENCES stores(id)
);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_no TEXT UNIQUE,
  full_name TEXT NOT NULL,
  phone TEXT,
  email TEXT,
  dob TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  supplier_code TEXT UNIQUE,
  name TEXT NOT NULL,
  phone TEXT,
  email TEXT,
  terms TEXT
);
CREATE TABLE IF NOT EXISTS prescriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rx_number TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  customer_id INTEGER,
  patient_name TEXT NOT NULL,
  prescriber TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  pharmacist_user_id INTEGER,
  created_at TEXT NOT NULL,
  checked_at TEXT,
  FOREIGN KEY(store_id) REFERENCES stores(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id),
  FOREIGN KEY(pharmacist_user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS prescription_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prescription_id INTEGER NOT NULL,
  product_id INTEGER,
  drug_name TEXT NOT NULL,
  sig TEXT NOT NULL,
  quantity REAL NOT NULL,
  refills INTEGER NOT NULL DEFAULT 0,
  auxiliary_warning TEXT,
  label_printed_at TEXT,
  FOREIGN KEY(prescription_id) REFERENCES prescriptions(id),
  FOREIGN KEY(product_id) REFERENCES products(id)
);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_no TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  customer_id INTEGER,
  subtotal REAL NOT NULL DEFAULT 0,
  discount REAL NOT NULL DEFAULT 0,
  tax REAL NOT NULL DEFAULT 0,
  total REAL NOT NULL DEFAULT 0,
  payment_method TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(store_id) REFERENCES stores(id),
  FOREIGN KEY(user_id) REFERENCES users(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS sale_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  batch_id INTEGER,
  quantity REAL NOT NULL,
  unit_price REAL NOT NULL,
  cost REAL NOT NULL DEFAULT 0,
  FOREIGN KEY(sale_id) REFERENCES sales(id),
  FOREIGN KEY(product_id) REFERENCES products(id),
  FOREIGN KEY(batch_id) REFERENCES batches(id)
);
CREATE TABLE IF NOT EXISTS stock_transfers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transfer_no TEXT UNIQUE NOT NULL,
  from_store_id INTEGER NOT NULL,
  to_store_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  received_at TEXT,
  FOREIGN KEY(from_store_id) REFERENCES stores(id),
  FOREIGN KEY(to_store_id) REFERENCES stores(id),
  FOREIGN KEY(created_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  details TEXT,
  created_at TEXT NOT NULL
);
'''

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    conn = db(); conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO stores(code,name) VALUES('CAR','Carenage Pharmacy')")
    conn.execute("INSERT OR IGNORE INTO stores(code,name) VALUES('ST2','Store 2')")
    store = conn.execute("SELECT id FROM stores WHERE code='CAR'").fetchone()['id']
    if not conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        conn.execute("INSERT INTO users(username,full_name,role,password_hash,default_store_id,created_at) VALUES(?,?,?,?,?,?)",
                     ('admin','Westmed Administrator','admin',generate_password_hash(os.environ.get('ADMIN_PASSWORD','CHANGE-ME')),store,datetime.utcnow().isoformat()))
    demo_products = [
        ('PCM500','100000000001','Paracetamol 500 mg','500 mg','Tablet','Analgesics',0,0,0.35,1.50,30),
        ('AMX500','100000000002','Amoxicillin 500 mg','500 mg','Capsule','Antibiotics',1,0,0.90,3.50,20),
        ('LOR10','100000000003','Loratadine 10 mg','10 mg','Tablet','Allergy',0,0,0.50,2.25,15)
    ]
    for p in demo_products:
        conn.execute('''INSERT OR IGNORE INTO products(sku,barcode,name,strength,dosage_form,category,prescription_required,controlled,cost,retail_price,reorder_level)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)''', p)
    conn.commit(); conn.close()

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return fn(*a, **kw)
    return wrapper

def audit(action, entity_type=None, entity_id=None, details=None):
    conn=db(); conn.execute('INSERT INTO audit_log(user_id,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)',
        (session.get('user_id'), action, entity_type, entity_id, details, datetime.utcnow().isoformat())); conn.commit(); conn.close()

@app.route('/login', methods=['GET','POST'])
def login():
    error=None
    if request.method=='POST':
        conn=db(); u=conn.execute('SELECT * FROM users WHERE username=? AND active=1',(request.form['username'],)).fetchone(); conn.close()
        if u and check_password_hash(u['password_hash'], request.form['password']):
            session.clear(); session['user_id']=u['id']; session['username']=u['username']; session['full_name']=u['full_name']; session['role']=u['role']; session['store_id']=u['default_store_id']
            audit('login')
            return redirect(url_for('home'))
        error='Invalid username or password.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

@app.route('/')
@login_required
def home():
    return render_template('index.html', user=session)

@app.get('/api/dashboard')
@login_required
def dashboard_api():
    conn=db()
    stores=[dict(r) for r in conn.execute('SELECT * FROM stores WHERE active=1')]
    total_products=conn.execute('SELECT COUNT(*) c FROM products WHERE active=1').fetchone()['c']
    expiring=conn.execute("SELECT COUNT(*) c FROM batches WHERE expiry_date IS NOT NULL AND date(expiry_date) <= date('now','+90 day') AND quantity>0").fetchone()['c']
    low_stock=conn.execute('''SELECT COUNT(*) c FROM (SELECT p.id, COALESCE(SUM(b.quantity),0) q, p.reorder_level FROM products p LEFT JOIN batches b ON b.product_id=p.id GROUP BY p.id HAVING q <= p.reorder_level)''').fetchone()['c']
    rx_open=conn.execute("SELECT COUNT(*) c FROM prescriptions WHERE status IN ('new','in_progress','awaiting_check','ready')").fetchone()['c']
    conn.close()
    return jsonify(stores=stores,total_products=total_products,expiring_90=expiring,low_stock=low_stock,open_prescriptions=rx_open)

@app.get('/api/products')
@login_required
def products_api():
    q=request.args.get('q','').strip(); conn=db()
    if q:
        rows=conn.execute('SELECT * FROM products WHERE active=1 AND (name LIKE ? OR sku LIKE ? OR barcode LIKE ?) ORDER BY name LIMIT 50',(f'%{q}%',f'%{q}%',f'%{q}%')).fetchall()
    else: rows=conn.execute('SELECT * FROM products WHERE active=1 ORDER BY name LIMIT 100').fetchall()
    out=[]
    for r in rows:
        x=dict(r); x['stock']=conn.execute('SELECT COALESCE(SUM(quantity),0) q FROM batches WHERE product_id=?',(r['id'],)).fetchone()['q']; out.append(x)
    conn.close(); return jsonify(out)

@app.get('/api/prescriptions')
@login_required
def prescriptions_api():
    conn=db(); rows=conn.execute('SELECT * FROM prescriptions ORDER BY id DESC LIMIT 50').fetchall(); conn.close(); return jsonify([dict(r) for r in rows])

@app.post('/api/prescriptions')
@login_required
def create_rx():
    data=request.get_json(force=True)
    conn=db(); now=datetime.utcnow().isoformat(); rx=data.get('rx_number') or ('RX-'+datetime.now().strftime('%y%m%d%H%M%S'))
    cur=conn.execute('INSERT INTO prescriptions(rx_number,store_id,patient_name,prescriber,status,pharmacist_user_id,created_at) VALUES(?,?,?,?,?,?,?)',
        (rx, data.get('store_id') or session.get('store_id'), data['patient_name'], data.get('prescriber'), 'awaiting_check', session['user_id'], now))
    rxid=cur.lastrowid
    conn.execute('INSERT INTO prescription_items(prescription_id,drug_name,sig,quantity,refills,auxiliary_warning) VALUES(?,?,?,?,?,?)',
        (rxid,data['drug_name'],data['sig'],data.get('quantity',1),data.get('refills',0),data.get('auxiliary_warning')))
    conn.commit(); conn.close(); audit('create_prescription','prescription',str(rxid),rx)
    return jsonify(ok=True,id=rxid,rx_number=rx)

@app.post('/api/prescriptions/<int:rxid>/status')
@login_required
def rx_status(rxid):
    data=request.get_json(force=True); status=data['status']; conn=db()
    checked=datetime.utcnow().isoformat() if status=='ready' else None
    conn.execute('UPDATE prescriptions SET status=?, checked_at=COALESCE(?,checked_at), pharmacist_user_id=? WHERE id=?',(status,checked,session['user_id'],rxid)); conn.commit(); conn.close(); audit('update_rx_status','prescription',str(rxid),status); return jsonify(ok=True)

@app.get('/api/inventory')
@login_required
def inventory_api():
    conn=db(); rows=conn.execute('''SELECT b.id,p.sku,p.name,s.name store,b.lot_number,b.expiry_date,b.quantity,b.unit_cost
      FROM batches b JOIN products p ON p.id=b.product_id JOIN stores s ON s.id=b.store_id
      ORDER BY CASE WHEN b.expiry_date IS NULL THEN 1 ELSE 0 END,b.expiry_date,p.name''').fetchall(); conn.close(); return jsonify([dict(r) for r in rows])

@app.get('/api/migration/status')
@login_required
def migration_status():
    conn=db(); counts={}
    for t in ['products','batches','customers','suppliers','sales','prescriptions']:
        counts[t]=conn.execute(f'SELECT COUNT(*) c FROM {t}').fetchone()['c']
    conn.close(); return jsonify(counts)

@app.post('/api/migration/import/<entity>')
@login_required
def migration_import(entity):
    if session.get('role')!='admin': return jsonify(error='Admin only'),403
    if 'file' not in request.files: return jsonify(error='CSV file required'),400
    f=request.files['file']; text=f.stream.read().decode('utf-8-sig'); reader=csv.DictReader(io.StringIO(text)); rows=list(reader); conn=db(); inserted=0; errors=[]
    try:
        if entity=='products':
            for i,r in enumerate(rows,2):
                try:
                    conn.execute('''INSERT OR REPLACE INTO products(sku,barcode,name,strength,dosage_form,category,prescription_required,controlled,cost,retail_price,reorder_level,active)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,1)''',(r['sku'],r.get('barcode'),r['name'],r.get('strength'),r.get('dosage_form'),r.get('category'),int(r.get('prescription_required') or 0),int(r.get('controlled') or 0),float(r.get('cost') or 0),float(r.get('retail_price') or 0),float(r.get('reorder_level') or 0))); inserted+=1
                except Exception as e: errors.append(f'row {i}: {e}')
        elif entity=='customers':
            for i,r in enumerate(rows,2):
                try:
                    conn.execute('INSERT OR REPLACE INTO customers(customer_no,full_name,phone,email,dob,notes) VALUES(?,?,?,?,?,?)',(r.get('customer_no'),r['full_name'],r.get('phone'),r.get('email'),r.get('dob'),r.get('notes'))); inserted+=1
                except Exception as e: errors.append(f'row {i}: {e}')
        elif entity=='suppliers':
            for i,r in enumerate(rows,2):
                try:
                    conn.execute('INSERT OR REPLACE INTO suppliers(supplier_code,name,phone,email,terms) VALUES(?,?,?,?,?)',(r.get('supplier_code'),r['name'],r.get('phone'),r.get('email'),r.get('terms'))); inserted+=1
                except Exception as e: errors.append(f'row {i}: {e}')
        else:
            return jsonify(error='Supported imports: products, customers, suppliers'),400
        conn.commit()
    finally: conn.close()
    audit('migration_import',entity,None,f'inserted={inserted}; errors={len(errors)}')
    return jsonify(ok=True,inserted=inserted,errors=errors[:20])

@app.get('/api/ai/context')
@login_required
def ai_context():
    conn=db()
    summary={
      'stores':[dict(r) for r in conn.execute('SELECT id,code,name FROM stores WHERE active=1')],
      'open_prescriptions':conn.execute("SELECT COUNT(*) c FROM prescriptions WHERE status!='collected'").fetchone()['c'],
      'expiring_90_days':conn.execute("SELECT COUNT(*) c FROM batches WHERE expiry_date IS NOT NULL AND date(expiry_date)<=date('now','+90 day') AND quantity>0").fetchone()['c']
    }; conn.close(); return jsonify(summary)

@app.get('/health')
def health(): return jsonify(status='ok',app='Westmed Pharmacy POS V2')

init_db()
register_v3(app, db, audit)
register_v5(app, db, audit)

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','5000')), debug=False)
