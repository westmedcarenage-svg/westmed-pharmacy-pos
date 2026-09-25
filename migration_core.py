import os, re, sqlite3, json, time
from datetime import datetime

RELEVANT={
'phppos_people','phppos_customers','phppos_suppliers','phppos_employees','phppos_categories','phppos_items','phppos_location_items','phppos_locations',
'phppos_sales','phppos_sales_items','phppos_sales_payments','phppos_receivings','phppos_receivings_items','phppos_price_rules','phppos_item_kits','phppos_item_kit_items',
'phppos_giftcards','phppos_expenses','phppos_employees_time_clock','phppos_sales_deliveries','phppos_registers','phppos_register_log','phppos_app_config'
}

BASE_SCHEMA = """
PRAGMA foreign_keys = OFF;
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
  role TEXT NOT NULL, password_hash TEXT NOT NULL, default_store_id INTEGER, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT UNIQUE NOT NULL, barcode TEXT, name TEXT NOT NULL,
  strength TEXT, dosage_form TEXT, category TEXT, prescription_required INTEGER NOT NULL DEFAULT 0,
  controlled INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0, retail_price REAL NOT NULL DEFAULT 0,
  reorder_level REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, store_id INTEGER NOT NULL,
  lot_number TEXT NOT NULL, expiry_date TEXT, quantity REAL NOT NULL DEFAULT 0, unit_cost REAL NOT NULL DEFAULT 0,
  received_at TEXT, UNIQUE(product_id,store_id,lot_number)
);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_no TEXT UNIQUE, full_name TEXT NOT NULL, phone TEXT, email TEXT, dob TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_code TEXT UNIQUE, name TEXT NOT NULL, phone TEXT, email TEXT, terms TEXT
);
CREATE TABLE IF NOT EXISTS prescriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rx_number TEXT UNIQUE NOT NULL, store_id INTEGER NOT NULL, customer_id INTEGER,
  patient_name TEXT NOT NULL, prescriber TEXT, status TEXT NOT NULL DEFAULT 'new', pharmacist_user_id INTEGER,
  created_at TEXT NOT NULL, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS prescription_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prescription_id INTEGER NOT NULL, product_id INTEGER, drug_name TEXT NOT NULL,
  sig TEXT NOT NULL, quantity REAL NOT NULL, refills INTEGER NOT NULL DEFAULT 0, auxiliary_warning TEXT, label_printed_at TEXT
);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sale_no TEXT UNIQUE NOT NULL, store_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  customer_id INTEGER, subtotal REAL NOT NULL DEFAULT 0, discount REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0,
  total REAL NOT NULL DEFAULT 0, payment_method TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sale_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL, batch_id INTEGER,
  quantity REAL NOT NULL, unit_price REAL NOT NULL, cost REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stock_transfers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, transfer_no TEXT UNIQUE NOT NULL, from_store_id INTEGER NOT NULL, to_store_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft', created_by INTEGER NOT NULL, created_at TEXT NOT NULL, received_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, entity_type TEXT, entity_id TEXT, details TEXT, created_at TEXT NOT NULL
);
"""


def iter_tuples(s):
    i=0;n=len(s)
    while i<n:
        while i<n and s[i]!='(': i+=1
        if i>=n: break
        start=i;depth=0;quote=False;esc=False
        while i<n:
            ch=s[i]
            if quote:
                if esc: esc=False
                elif ch=='\\': esc=True
                elif ch=="'": quote=False
            else:
                if ch=="'": quote=True
                elif ch=='(': depth+=1
                elif ch==')':
                    depth-=1
                    if depth==0:
                        yield s[start:i+1]; i+=1; break
            i+=1

def split_fields(t):
    inner=t[1:-1];out=[];cur=[];quote=False;esc=False
    for ch in inner:
        if quote:
            cur.append(ch)
            if esc: esc=False
            elif ch=='\\': esc=True
            elif ch=="'": quote=False
        else:
            if ch=="'": quote=True;cur.append(ch)
            elif ch==',': out.append(''.join(cur).strip());cur=[]
            else: cur.append(ch)
    out.append(''.join(cur).strip());return out

def mysql_value(x):
    if x=='NULL': return None
    if x.startswith("'") and x.endswith("'"):
        b=x[1:-1]; repl={'0':'\x00','b':'\b','n':'\n','r':'\r','t':'\t','Z':'\x1a',"'":"'",'"':'"','\\':'\\'}; out=[];i=0
        while i<len(b):
            if b[i]=='\\' and i+1<len(b): out.append(repl.get(b[i+1],b[i+1]));i+=2
            else: out.append(b[i]);i+=1
        return ''.join(out)
    try:
        if any(c in x for c in '.eE'): return float(x)
        return int(x)
    except: return x

def schemas_from_dump(path):
    cols={};cur=None
    with open(path,'r',encoding='utf-8',errors='replace') as f:
        for line in f:
            m=re.match(r'CREATE TABLE `([^`]+)`',line)
            if m: cur=m.group(1);cols[cur]=[];continue
            if cur:
                m=re.match(r'  `([^`]+)` ',line)
                if m: cols[cur].append(m.group(1))
                if line.startswith(') ENGINE='): cur=None
    return cols

def ensure_v5_schema(conn):
    conn.executescript(BASE_SCHEMA)
    conn.executescript("""
    PRAGMA foreign_keys=OFF;
    CREATE TABLE IF NOT EXISTS v5_sales_history(sale_id INTEGER PRIMARY KEY,sale_time TEXT,customer_id INTEGER,customer_name TEXT,employee_id INTEGER,employee_name TEXT,sold_by_employee_id INTEGER,sold_by_name TEXT,comment TEXT,payment_type TEXT,deleted INTEGER,suspended INTEGER,location_id INTEGER,location_name TEXT,register_id INTEGER,total_quantity REAL,subtotal REAL,tax REAL,total REAL,profit REAL,return_sale_id INTEGER,tip REAL);
    CREATE TABLE IF NOT EXISTS v5_sale_items(id INTEGER PRIMARY KEY AUTOINCREMENT,sale_id INTEGER,item_id INTEGER,item_name TEXT,line INTEGER,quantity REAL,quantity_received REAL,item_cost_price REAL,item_unit_price REAL,regular_unit_price REAL,discount_percent REAL,subtotal REAL,tax REAL,total REAL,profit REAL);
    CREATE INDEX IF NOT EXISTS idx_v5_sale_items_sale ON v5_sale_items(sale_id);
    CREATE INDEX IF NOT EXISTS idx_v5_sale_items_item ON v5_sale_items(item_id);
    CREATE TABLE IF NOT EXISTS v5_sale_payments(payment_id INTEGER PRIMARY KEY,sale_id INTEGER,payment_type TEXT,payment_amount REAL,payment_date TEXT,card_issuer TEXT,truncated_card TEXT,auth_code TEXT,ref_no TEXT);
    CREATE INDEX IF NOT EXISTS idx_v5_sale_payments_sale ON v5_sale_payments(sale_id);
    CREATE TABLE IF NOT EXISTS v5_receivings_history(receiving_id INTEGER PRIMARY KEY,receiving_time TEXT,supplier_id INTEGER,supplier_name TEXT,employee_id INTEGER,employee_name TEXT,comment TEXT,payment_type TEXT,deleted INTEGER,suspended INTEGER,location_id INTEGER,location_name TEXT,transfer_to_location_id INTEGER,is_po INTEGER,total_quantity_purchased REAL,total_quantity_received REAL,subtotal REAL,tax REAL,total REAL,profit REAL,shipping_cost REAL);
    CREATE TABLE IF NOT EXISTS v5_receiving_items(id INTEGER PRIMARY KEY AUTOINCREMENT,receiving_id INTEGER,item_id INTEGER,item_name TEXT,line INTEGER,quantity_purchased REAL,quantity_received REAL,item_cost_price REAL,item_unit_price REAL,discount_percent REAL,expire_date TEXT,subtotal REAL,tax REAL,total REAL,profit REAL);
    CREATE INDEX IF NOT EXISTS idx_v5_receiving_items_receiving ON v5_receiving_items(receiving_id);
    CREATE TABLE IF NOT EXISTS v5_employees(person_id INTEGER PRIMARY KEY,username TEXT,full_name TEXT,phone TEXT,email TEXT,employee_number TEXT,hourly_pay_rate REAL,hire_date TEXT,inactive INTEGER,deleted INTEGER,max_discount_percent REAL);
    CREATE TABLE IF NOT EXISTS v5_price_rules(id INTEGER PRIMARY KEY,name TEXT,start_date TEXT,end_date TEXT,active INTEGER,deleted INTEGER,type TEXT,items_to_buy REAL,items_to_get REAL,percent_off REAL,fixed_off REAL,spend_amount REAL,num_times_to_apply INTEGER,coupon_code TEXT,description TEXT,show_on_receipt INTEGER,mix_and_match INTEGER);
    CREATE TABLE IF NOT EXISTS v5_item_kits(item_kit_id INTEGER PRIMARY KEY,item_kit_number TEXT,product_id TEXT,name TEXT,description TEXT,unit_price REAL,cost_price REAL,deleted INTEGER,inactive INTEGER);
    CREATE TABLE IF NOT EXISTS v5_item_kit_items(id INTEGER PRIMARY KEY,item_kit_id INTEGER,item_id INTEGER,item_name TEXT,quantity REAL);
    CREATE TABLE IF NOT EXISTS v5_giftcards(giftcard_id INTEGER PRIMARY KEY,giftcard_number TEXT,description TEXT,value REAL,customer_id INTEGER,customer_name TEXT,inactive INTEGER,deleted INTEGER);
    CREATE TABLE IF NOT EXISTS v5_expenses(id INTEGER PRIMARY KEY,location_id INTEGER,location_name TEXT,expense_type TEXT,description TEXT,reason TEXT,expense_date TEXT,amount REAL,tax REAL,note TEXT,employee_id INTEGER,employee_name TEXT,payment_type TEXT,deleted INTEGER);
    CREATE TABLE IF NOT EXISTS v5_time_clock(id INTEGER PRIMARY KEY,employee_id INTEGER,employee_name TEXT,location_id INTEGER,location_name TEXT,clock_in TEXT,clock_out TEXT,clock_in_comment TEXT,clock_out_comment TEXT,hourly_pay_rate REAL);
    CREATE TABLE IF NOT EXISTS v5_deliveries(id INTEGER PRIMARY KEY,sale_id INTEGER,status TEXT,estimated_shipping_date TEXT,actual_shipping_date TEXT,estimated_delivery_or_pickup_date TEXT,actual_delivery_or_pickup_date TEXT,is_pickup INTEGER,tracking_number TEXT,comment TEXT,deleted INTEGER,delivery_employee_person_id INTEGER,delivery_employee_name TEXT);
    CREATE TABLE IF NOT EXISTS v5_registers(register_id INTEGER PRIMARY KEY,location_id INTEGER,location_name TEXT,name TEXT,deleted INTEGER);
    CREATE TABLE IF NOT EXISTS v5_register_log(register_log_id INTEGER PRIMARY KEY,employee_id_open INTEGER,employee_open_name TEXT,employee_id_close INTEGER,employee_close_name TEXT,register_id INTEGER,shift_start TEXT,shift_end TEXT,notes TEXT,deleted INTEGER);
    CREATE TABLE IF NOT EXISTS v5_app_config(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS v5_migration_meta(key TEXT PRIMARY KEY,value TEXT);
    """)
    conn.commit()

def migrate(sql_path, db_path, progress=None):
    started=time.time(); cols=schemas_from_dump(sql_path)
    conn=sqlite3.connect(db_path, timeout=120);conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA busy_timeout=120000');conn.execute('PRAGMA journal_mode=MEMORY');conn.execute('PRAGMA synchronous=OFF');conn.execute('PRAGMA temp_store=MEMORY')
    ensure_v5_schema(conn)
    for t in ['v5_sales_history','v5_sale_items','v5_sale_payments','v5_receivings_history','v5_receiving_items','v5_employees','v5_price_rules','v5_item_kits','v5_item_kit_items','v5_giftcards','v5_expenses','v5_time_clock','v5_deliveries','v5_registers','v5_register_log','v5_app_config']:
        conn.execute(f'DELETE FROM {t}')
    conn.execute('DELETE FROM batches');conn.execute('DELETE FROM products');conn.execute('DELETE FROM customers');conn.execute('DELETE FROM suppliers');conn.commit()
    people={};cats={};locations={};customer_rows=[];supplier_rows=[];employee_rows=[];items={};counts={};buffers={}
    def emit(table,sql,row,size=2000):
        key=(table,sql);b=buffers.setdefault(key,[]);b.append(row)
        if len(b)>=size:
            conn.executemany(sql,b);counts[table]=counts.get(table,0)+len(b);b.clear()
    def flush():
        for (table,sql),b in list(buffers.items()):
            if b: conn.executemany(sql,b);counts[table]=counts.get(table,0)+len(b);b.clear()
        conn.commit()
    def g(d,k,default=None): return d.get(k,default)
    total_size=os.path.getsize(sql_path);done=0;last_report=0
    with open(sql_path,'r',encoding='utf-8',errors='replace',newline='') as f:
        for line in f:
            done+=len(line)
            if progress and done-last_report>5_000_000:
                progress({'phase':'parsing','percent':round(done*100/total_size,1),'counts':dict(counts)});last_report=done
            if not line.startswith('INSERT INTO `'): continue
            m=re.match(r"INSERT INTO `([^`]+)` VALUES (.*);\s*$",line,re.S)
            if not m: continue
            table=m.group(1)
            if table not in RELEVANT: continue
            c=cols.get(table,[])
            for tup in iter_tuples(m.group(2)):
                vals=[mysql_value(x) for x in split_fields(tup)]
                if len(vals)!=len(c): continue
                d=dict(zip(c,vals))
                if table=='phppos_people':
                    people[g(d,'person_id')]=d
                elif table=='phppos_categories':
                    cats[g(d,'id')]=g(d,'name')
                elif table=='phppos_locations':
                    lid=g(d,'location_id');locations[lid]=g(d,'name')
                    conn.execute('INSERT OR REPLACE INTO stores(id,code,name,active) VALUES(?,?,?,?)',(lid,f'LOC{lid}',g(d,'name') or f'Location {lid}',0 if g(d,'deleted',0) else 1))
                elif table=='phppos_customers': customer_rows.append(d)
                elif table=='phppos_suppliers': supplier_rows.append(d)
                elif table=='phppos_employees': employee_rows.append(d)
                elif table=='phppos_items':
                    iid=g(d,'item_id');items[iid]=g(d,'name');sku=g(d,'item_number') or g(d,'product_id') or f'ITEM-{iid}'
                    emit('products','INSERT OR REPLACE INTO products(id,sku,barcode,name,strength,dosage_form,category,prescription_required,controlled,cost,retail_price,reorder_level,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(iid,str(sku),g(d,'item_number'),g(d,'name') or f'Item {iid}',g(d,'size'),None,cats.get(g(d,'category_id')),0,0,g(d,'cost_price') or 0,g(d,'unit_price') or 0,g(d,'reorder_level') or 0,0 if g(d,'deleted',0) or g(d,'item_inactive',0) else 1))
                elif table=='phppos_location_items':
                    q=g(d,'quantity') or 0
                    if q!=0: emit('batches','INSERT OR REPLACE INTO batches(product_id,store_id,lot_number,expiry_date,quantity,unit_cost,received_at) VALUES(?,?,?,?,?,?,?)',(g(d,'item_id'),g(d,'location_id'),'MIGRATED',None,q,g(d,'cost_price') or 0,datetime.utcnow().isoformat()))
                elif table=='phppos_sales':
                    cid=g(d,'customer_id');eid=g(d,'employee_id');sid=g(d,'sold_by_employee_id');loc=g(d,'location_id');pn=people.get(cid,{});en=people.get(eid,{});sn=people.get(sid,{})
                    emit('v5_sales_history','INSERT OR REPLACE INTO v5_sales_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(g(d,'sale_id'),g(d,'sale_time'),cid,pn.get('full_name'),eid,en.get('full_name'),sid,sn.get('full_name'),g(d,'comment'),g(d,'payment_type'),g(d,'deleted',0),g(d,'suspended',0),loc,locations.get(loc),g(d,'register_id'),g(d,'total_quantity_purchased'),g(d,'subtotal'),g(d,'tax'),g(d,'total'),g(d,'profit'),g(d,'return_sale_id'),g(d,'tip')))
                elif table=='phppos_sales_items':
                    iid=g(d,'item_id')
                    emit('v5_sale_items','INSERT INTO v5_sale_items(sale_id,item_id,item_name,line,quantity,quantity_received,item_cost_price,item_unit_price,regular_unit_price,discount_percent,subtotal,tax,total,profit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(g(d,'sale_id'),iid,items.get(iid),g(d,'line'),g(d,'quantity_purchased'),g(d,'quantity_received'),g(d,'item_cost_price'),g(d,'item_unit_price'),g(d,'regular_item_unit_price_at_time_of_sale'),g(d,'discount_percent'),g(d,'subtotal'),g(d,'tax'),g(d,'total'),g(d,'profit')))
                elif table=='phppos_sales_payments':
                    emit('v5_sale_payments','INSERT OR REPLACE INTO v5_sale_payments VALUES(?,?,?,?,?,?,?,?,?)',(g(d,'payment_id'),g(d,'sale_id'),g(d,'payment_type'),g(d,'payment_amount'),g(d,'payment_date'),g(d,'card_issuer'),g(d,'truncated_card'),g(d,'auth_code'),g(d,'ref_no')))
                elif table=='phppos_receivings':
                    sup=g(d,'supplier_id');emp=g(d,'employee_id');loc=g(d,'location_id');sp=people.get(sup,{});ep=people.get(emp,{})
                    emit('v5_receivings_history','INSERT OR REPLACE INTO v5_receivings_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(g(d,'receiving_id'),g(d,'receiving_time'),sup,sp.get('full_name'),emp,ep.get('full_name'),g(d,'comment'),g(d,'payment_type'),g(d,'deleted',0),g(d,'suspended',0),loc,locations.get(loc),g(d,'transfer_to_location_id'),g(d,'is_po'),g(d,'total_quantity_purchased'),g(d,'total_quantity_received'),g(d,'subtotal'),g(d,'tax'),g(d,'total'),g(d,'profit'),g(d,'shipping_cost')))
                elif table=='phppos_receivings_items':
                    iid=g(d,'item_id')
                    emit('v5_receiving_items','INSERT INTO v5_receiving_items(receiving_id,item_id,item_name,line,quantity_purchased,quantity_received,item_cost_price,item_unit_price,discount_percent,expire_date,subtotal,tax,total,profit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(g(d,'receiving_id'),iid,items.get(iid),g(d,'line'),g(d,'quantity_purchased'),g(d,'quantity_received'),g(d,'item_cost_price'),g(d,'item_unit_price'),g(d,'discount_percent'),g(d,'expire_date'),g(d,'subtotal'),g(d,'tax'),g(d,'total'),g(d,'profit')))
                elif table=='phppos_price_rules':
                    emit('v5_price_rules','INSERT OR REPLACE INTO v5_price_rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(g(d,'id'),g(d,'name'),g(d,'start_date'),g(d,'end_date'),g(d,'active'),g(d,'deleted'),g(d,'type'),g(d,'items_to_buy'),g(d,'items_to_get'),g(d,'percent_off'),g(d,'fixed_off'),g(d,'spend_amount'),g(d,'num_times_to_apply'),g(d,'coupon_code'),g(d,'description'),g(d,'show_on_receipt'),g(d,'mix_and_match')))
                elif table=='phppos_item_kits':
                    emit('v5_item_kits','INSERT OR REPLACE INTO v5_item_kits VALUES(?,?,?,?,?,?,?,?,?)',(g(d,'item_kit_id'),g(d,'item_kit_number'),g(d,'product_id'),g(d,'name'),g(d,'description'),g(d,'unit_price'),g(d,'cost_price'),g(d,'deleted'),g(d,'item_kit_inactive')))
                elif table=='phppos_item_kit_items':
                    iid=g(d,'item_id');emit('v5_item_kit_items','INSERT OR REPLACE INTO v5_item_kit_items VALUES(?,?,?,?,?)',(g(d,'id'),g(d,'item_kit_id'),iid,items.get(iid),g(d,'quantity')))
                elif table=='phppos_giftcards':
                    cid=g(d,'customer_id');pn=people.get(cid,{})
                    emit('v5_giftcards','INSERT OR REPLACE INTO v5_giftcards VALUES(?,?,?,?,?,?,?,?)',(g(d,'giftcard_id'),g(d,'giftcard_number'),g(d,'description'),g(d,'value'),cid,pn.get('full_name'),g(d,'inactive'),g(d,'deleted')))
                elif table=='phppos_expenses':
                    eid=g(d,'employee_id');ep=people.get(eid,{});loc=g(d,'location_id')
                    emit('v5_expenses','INSERT OR REPLACE INTO v5_expenses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(g(d,'id'),loc,locations.get(loc),g(d,'expense_type'),g(d,'expense_description'),g(d,'expense_reason'),g(d,'expense_date'),g(d,'expense_amount'),g(d,'expense_tax'),g(d,'expense_note'),eid,ep.get('full_name'),g(d,'expense_payment_type'),g(d,'deleted')))
                elif table=='phppos_employees_time_clock':
                    eid=g(d,'employee_id');ep=people.get(eid,{});loc=g(d,'location_id')
                    emit('v5_time_clock','INSERT OR REPLACE INTO v5_time_clock VALUES(?,?,?,?,?,?,?,?,?,?)',(g(d,'id'),eid,ep.get('full_name'),loc,locations.get(loc),g(d,'clock_in'),g(d,'clock_out'),g(d,'clock_in_comment'),g(d,'clock_out_comment'),g(d,'hourly_pay_rate')))
                elif table=='phppos_sales_deliveries':
                    eid=g(d,'delivery_employee_person_id');ep=people.get(eid,{})
                    emit('v5_deliveries','INSERT OR REPLACE INTO v5_deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(g(d,'id'),g(d,'sale_id'),g(d,'status'),g(d,'estimated_shipping_date'),g(d,'actual_shipping_date'),g(d,'estimated_delivery_or_pickup_date'),g(d,'actual_delivery_or_pickup_date'),g(d,'is_pickup'),g(d,'tracking_number'),g(d,'comment'),g(d,'deleted'),eid,ep.get('full_name')))
                elif table=='phppos_registers':
                    loc=g(d,'location_id');emit('v5_registers','INSERT OR REPLACE INTO v5_registers VALUES(?,?,?,?,?)',(g(d,'register_id'),loc,locations.get(loc),g(d,'name'),g(d,'deleted')))
                elif table=='phppos_register_log':
                    eo=g(d,'employee_id_open');ec=g(d,'employee_id_close')
                    emit('v5_register_log','INSERT OR REPLACE INTO v5_register_log VALUES(?,?,?,?,?,?,?,?,?,?)',(g(d,'register_log_id'),eo,people.get(eo,{}).get('full_name'),ec,people.get(ec,{}).get('full_name'),g(d,'register_id'),g(d,'shift_start'),g(d,'shift_end'),g(d,'notes'),g(d,'deleted')))
                elif table=='phppos_app_config':
                    emit('v5_app_config','INSERT OR REPLACE INTO v5_app_config(key,value) VALUES(?,?)',(g(d,'key'),g(d,'value')))
    for d in customer_rows:
        pid=g(d,'person_id');p=people.get(pid,{})
        emit('customers','INSERT OR REPLACE INTO customers(id,customer_no,full_name,phone,email,dob,notes) VALUES(?,?,?,?,?,?,?)',(pid,g(d,'account_number'),p.get('full_name') or (p.get('first_name','')+' '+p.get('last_name','')).strip(),p.get('phone_number'),p.get('email'),None,(g(d,'internal_notes') or p.get('comments') or '')))
    for d in supplier_rows:
        pid=g(d,'person_id');p=people.get(pid,{})
        emit('suppliers','INSERT OR REPLACE INTO suppliers(id,supplier_code,name,phone,email,terms) VALUES(?,?,?,?,?,?)',(pid,g(d,'account_number'),g(d,'company_name') or p.get('full_name'),p.get('phone_number'),p.get('email'),None))
    for d in employee_rows:
        pid=g(d,'person_id');p=people.get(pid,{})
        emit('v5_employees','INSERT OR REPLACE INTO v5_employees VALUES(?,?,?,?,?,?,?,?,?,?,?)',(pid,g(d,'username'),p.get('full_name') or (p.get('first_name','')+' '+p.get('last_name','')).strip(),p.get('phone_number'),p.get('email'),g(d,'employee_number'),g(d,'hourly_pay_rate'),g(d,'hire_date'),g(d,'inactive'),g(d,'deleted'),g(d,'max_discount_percent')))
    flush()
    conn.execute("INSERT OR REPLACE INTO v5_migration_meta(key,value) VALUES('source_file',?)",(os.path.basename(sql_path),))
    conn.execute("INSERT OR REPLACE INTO v5_migration_meta(key,value) VALUES('migrated_at',?)",(datetime.utcnow().isoformat(),))
    conn.execute("INSERT OR REPLACE INTO v5_migration_meta(key,value) VALUES('duration_seconds',?)",(str(round(time.time()-started,2)),))
    conn.execute("INSERT OR REPLACE INTO v5_migration_meta(key,value) VALUES('counts_json',?)",(json.dumps(counts),))
    conn.commit();conn.close()
    if progress: progress({'phase':'complete','percent':100,'counts':counts})
    return counts