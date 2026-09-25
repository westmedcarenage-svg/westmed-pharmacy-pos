from flask import Blueprint, jsonify, request, session
from datetime import datetime
from werkzeug.security import generate_password_hash
import os, json, threading, sqlite3, shutil
from migration_core import migrate, ensure_v5_schema

bp5=Blueprint('v5',__name__,url_prefix='/api/v5')
MIGRATION_LOCK=threading.Lock()
MIGRATION_STATE={'phase':'idle','percent':0,'message':'No migration running','counts':{}}

def register_v5(app,db_fn,audit_fn):
    conn=db_fn();ensure_v5_schema(conn);conn.close()

    def rows(sql,params=()):
        c=db_fn();r=[dict(x) for x in c.execute(sql,params).fetchall()];c.close();return r
    def one(sql,params=()):
        c=db_fn();r=c.execute(sql,params).fetchone();c.close();return dict(r) if r else None
    def require_admin():
        return session.get('role')=='admin'

    @bp5.before_request
    def auth():
        if request.endpoint and request.endpoint.endswith('migration_status_public'):
            return None
        if 'user_id' not in session:
            return jsonify(error='Authentication required'),401

    @bp5.get('/dashboard')
    def dashboard():
        c=db_fn()
        hist_sales=c.execute("SELECT COUNT(*) c,COALESCE(SUM(total),0) total FROM v5_sales_history WHERE deleted=0 AND suspended=0").fetchone()
        cur_sales=c.execute("SELECT COUNT(*) c,COALESCE(SUM(total),0) total FROM sales").fetchone()
        out={
          'products':c.execute("SELECT COUNT(*) c FROM products WHERE active=1").fetchone()['c'],
          'customers':c.execute("SELECT COUNT(*) c FROM customers").fetchone()['c'],
          'suppliers':c.execute("SELECT COUNT(*) c FROM suppliers").fetchone()['c'],
          'historical_sales':hist_sales['c'],
          'current_sales':cur_sales['c'],
          'sales_total':float(hist_sales['total'] or 0)+float(cur_sales['total'] or 0),
          'open_rx':c.execute("SELECT COUNT(*) c FROM prescriptions WHERE status NOT IN ('collected','cancelled')").fetchone()['c'],
          'expiring_90':c.execute("SELECT COUNT(*) c FROM batches WHERE expiry_date IS NOT NULL AND date(expiry_date)<=date('now','+90 day') AND quantity>0").fetchone()['c'],
          'low_stock':c.execute("""SELECT COUNT(*) c FROM (SELECT p.id,p.reorder_level,COALESCE(SUM(b.quantity),0) q FROM products p LEFT JOIN batches b ON b.product_id=p.id GROUP BY p.id HAVING q<=COALESCE(p.reorder_level,0))""").fetchone()['c']
        };c.close();return jsonify(out)

    @bp5.get('/sales-history')
    def sales_history():
        limit=min(int(request.args.get('limit',500)),2000)
        return jsonify(rows("""SELECT sale_id AS id,'PHP-'||sale_id AS sale_no,location_name AS store,employee_name AS cashier,customer_name AS customer,total,payment_type,sale_time AS created_at,profit,subtotal,tax
          FROM v5_sales_history WHERE deleted=0 ORDER BY sale_id DESC LIMIT ?""",(limit,)))

    @bp5.get('/sale/<int:sale_id>')
    def sale_detail(sale_id):
        sale=one("SELECT * FROM v5_sales_history WHERE sale_id=?",(sale_id,))
        if not sale:return jsonify(error='Sale not found'),404
        items=rows("SELECT * FROM v5_sale_items WHERE sale_id=? ORDER BY line",(sale_id,))
        pays=rows("SELECT * FROM v5_sale_payments WHERE sale_id=? ORDER BY payment_id",(sale_id,))
        return jsonify(sale=sale,items=items,payments=pays)

    @bp5.post('/sales/complete')
    def complete_sale():
        d=request.get_json(force=True);items=d.get('items') or []
        if not items:return jsonify(error='Sale has no items'),400
        store_id=int(d.get('store_id') or session.get('store_id') or 1)
        customer_id=d.get('customer_id');payment=d.get('payment_method') or 'Cash'
        conn=db_fn();now=datetime.utcnow().isoformat();sale_no='WMP-'+datetime.now().strftime('%y%m%d%H%M%S%f')[:17]
        subtotal=0.0;prepared=[]
        for x in items:
            pid=int(x['product_id']);qty=float(x.get('quantity') or 1)
            p=conn.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
            if not p:conn.close();return jsonify(error=f'Item {pid} not found'),400
            price=float(x.get('unit_price') if x.get('unit_price') is not None else p['retail_price'])
            subtotal+=qty*price;prepared.append((p,qty,price))
        tax=float(d.get('tax') or 0);discount=float(d.get('discount') or 0);total=subtotal-discount+tax
        cur=conn.execute("INSERT INTO sales(sale_no,store_id,user_id,customer_id,subtotal,discount,tax,total,payment_method,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (sale_no,store_id,session['user_id'],customer_id,subtotal,discount,tax,total,payment,now))
        sid=cur.lastrowid
        for p,qty,price in prepared:
            remain=qty
            batches=conn.execute("""SELECT * FROM batches WHERE product_id=? AND store_id=? AND quantity>0 ORDER BY CASE WHEN expiry_date IS NULL THEN 1 ELSE 0 END,expiry_date,id""",(p['id'],store_id)).fetchall()
            if sum(float(b['quantity']) for b in batches)+1e-9<qty:
                conn.rollback();conn.close();return jsonify(error=f"Insufficient stock: {p['name']}"),400
            for b in batches:
                if remain<=0:break
                take=min(remain,float(b['quantity']))
                conn.execute("UPDATE batches SET quantity=quantity-? WHERE id=?",(take,b['id']))
                conn.execute("INSERT INTO sale_items(sale_id,product_id,batch_id,quantity,unit_price,cost) VALUES(?,?,?,?,?,?)",(sid,p['id'],b['id'],take,price,float(b['unit_cost'] or p['cost'] or 0)))
                remain-=take
        conn.commit();conn.close();audit_fn('complete_sale','sale',str(sid),sale_no)
        return jsonify(ok=True,id=sid,sale_no=sale_no,total=total)

    @bp5.get('/receiving-history')
    def receiving_history():
        return jsonify(rows("""SELECT receiving_id,receiving_time,supplier_name,employee_name,location_name,total,payment_type,is_po,total_quantity_received
          FROM v5_receivings_history WHERE deleted=0 ORDER BY receiving_id DESC LIMIT 1000"""))

    @bp5.get('/employees')
    def employees():
        return jsonify(rows("SELECT * FROM v5_employees WHERE deleted=0 ORDER BY full_name"))

    @bp5.get('/price-rules')
    def price_rules():
        return jsonify(rows("SELECT * FROM v5_price_rules WHERE deleted=0 ORDER BY id DESC"))

    @bp5.get('/item-kits')
    def item_kits():
        return jsonify(rows("SELECT * FROM v5_item_kits WHERE deleted=0 ORDER BY name"))

    @bp5.get('/gift-cards')
    def gift_cards():
        return jsonify(rows("SELECT * FROM v5_giftcards WHERE deleted=0 ORDER BY giftcard_id DESC"))

    @bp5.get('/expenses')
    def expenses():
        return jsonify(rows("SELECT * FROM v5_expenses WHERE deleted=0 ORDER BY expense_date DESC,id DESC LIMIT 2000"))

    @bp5.get('/time-clock')
    def time_clock():
        return jsonify(rows("SELECT * FROM v5_time_clock ORDER BY id DESC LIMIT 3000"))

    @bp5.get('/deliveries')
    def deliveries():
        return jsonify(rows("SELECT * FROM v5_deliveries WHERE deleted=0 ORDER BY id DESC LIMIT 2000"))

    @bp5.get('/reports/summary')
    def reports_summary():
        c=db_fn()
        sales=c.execute("SELECT COUNT(*) c,COALESCE(SUM(total),0) total,COALESCE(SUM(profit),0) profit FROM v5_sales_history WHERE deleted=0 AND suspended=0").fetchone()
        rec=c.execute("SELECT COUNT(*) c,COALESCE(SUM(total),0) total FROM v5_receivings_history WHERE deleted=0").fetchone()
        exp=c.execute("SELECT COALESCE(SUM(amount),0) total FROM v5_expenses WHERE deleted=0").fetchone()
        out={'sales_count':sales['c'],'sales_total':sales['total'],'gross_profit':sales['profit'],'receivings_count':rec['c'],'receivings_total':rec['total'],'expenses_total':exp['total'],
             'inventory_cost_value':c.execute("SELECT COALESCE(SUM(quantity*unit_cost),0) v FROM batches").fetchone()['v'],
             'customers':c.execute("SELECT COUNT(*) c FROM customers").fetchone()['c'],
             'items':c.execute("SELECT COUNT(*) c FROM products").fetchone()['c']}
        c.close();return jsonify(out)

    @bp5.get('/migration/status')
    def migration_status():
        c=db_fn()
        meta={r['key']:r['value'] for r in c.execute("SELECT key,value FROM v5_migration_meta").fetchall()}
        c.close()
        return jsonify(state=dict(MIGRATION_STATE),meta=meta,backup_exists=os.path.exists('/data/php_point_of_sale_backup.sql'),backup_size=os.path.getsize('/data/php_point_of_sale_backup.sql') if os.path.exists('/data/php_point_of_sale_backup.sql') else 0)

    @bp5.post('/migration/upload/reset')
    def migration_upload_reset():
        if not require_admin():return jsonify(error='Admin only'),403
        os.makedirs('/data',exist_ok=True)
        for p in ['/data/php_point_of_sale_backup.sql.part','/data/php_point_of_sale_backup.sql']:
            try:os.remove(p)
            except FileNotFoundError:pass
        return jsonify(ok=True)

    @bp5.post('/migration/upload/chunk')
    def migration_upload_chunk():
        if not require_admin():return jsonify(error='Admin only'),403
        os.makedirs('/data',exist_ok=True);part='/data/php_point_of_sale_backup.sql.part'
        offset=int(request.args.get('offset','0'))
        current=os.path.getsize(part) if os.path.exists(part) else 0
        if offset!=current:return jsonify(error='Upload offset mismatch',expected=current),409
        data=request.get_data(cache=False)
        with open(part,'ab') as f:f.write(data)
        return jsonify(ok=True,size=current+len(data))

    @bp5.post('/migration/upload/finish')
    def migration_upload_finish():
        if not require_admin():return jsonify(error='Admin only'),403
        part='/data/php_point_of_sale_backup.sql.part';final='/data/php_point_of_sale_backup.sql'
        if not os.path.exists(part):return jsonify(error='No uploaded backup'),400
        os.replace(part,final);return jsonify(ok=True,size=os.path.getsize(final))

    @bp5.post('/migration/run')
    def migration_run():
        if not require_admin():return jsonify(error='Admin only'),403
        backup='/data/php_point_of_sale_backup.sql'
        if not os.path.exists(backup):return jsonify(error='Upload the PHP POS SQL backup first'),400
        if MIGRATION_LOCK.locked():return jsonify(error='Migration already running'),409
        db_path=app.config.get('WESTMED_DB_PATH') or os.environ.get('WESTMED_DB_PATH') or '/data/westmed_v5.db'
        stage_path='/tmp/westmed_v5_migrating.db'
        def worker():
            global MIGRATION_STATE
            with MIGRATION_LOCK:
                try:
                    MIGRATION_STATE={'phase':'starting','percent':0,'message':'Preparing isolated staging database','counts':{}}
                    try: os.remove(stage_path)
                    except FileNotFoundError: pass
                    sc=sqlite3.connect(stage_path)
                    sc.row_factory=sqlite3.Row
                    ensure_v5_schema(sc)
                    sc.execute("INSERT OR IGNORE INTO stores(code,name,active) VALUES('CAR','Carenage Pharmacy',1)")
                    store_row=sc.execute("SELECT id FROM stores WHERE code='CAR'").fetchone()
                    store_id=store_row['id'] if store_row else 1
                    sc.execute("INSERT OR IGNORE INTO users(username,full_name,role,password_hash,default_store_id,active,created_at) VALUES(?,?,?,?,?,?,?)",
                               ('admin','Westmed Administrator','admin',generate_password_hash(os.environ.get('ADMIN_PASSWORD','CHANGE-ME')),store_id,1,datetime.utcnow().isoformat()))
                    sc.commit(); sc.close()
                    def prog(s):
                        global MIGRATION_STATE
                        MIGRATION_STATE={'phase':s.get('phase'),'percent':s.get('percent',0),'message':'Migrating PHP POS data into staging database','counts':s.get('counts',{})}
                    migrate(backup,stage_path,prog)
                    check=sqlite3.connect(stage_path)
                    integrity=check.execute("PRAGMA integrity_check").fetchone()[0]
                    check.close()
                    if integrity!='ok':
                        raise RuntimeError('Migrated staging database failed integrity check: '+str(integrity))
                    stage_size=os.path.getsize(stage_path)
                    if stage_size>470*1024*1024:
                        raise RuntimeError('Migrated database is too large for the current 500 MB Railway volume. Size: %.1f MB' % (stage_size/1024/1024))
                    MIGRATION_STATE={'phase':'switching','percent':100,'message':'Freeing backup space and promoting migrated database','counts':MIGRATION_STATE.get('counts',{})}
                    try:
                        os.remove(backup)
                    except FileNotFoundError:
                        pass
                    for suffix in ['', '-wal', '-shm', '-journal']:
                        try: os.remove(db_path+suffix)
                        except FileNotFoundError: pass
                    shutil.copy2(stage_path,db_path)
                    os.remove(stage_path)
                    MIGRATION_STATE={'phase':'complete','percent':100,'message':'Migration completed successfully','counts':MIGRATION_STATE.get('counts',{})}
                except Exception as e:
                    try: os.remove(stage_path)
                    except Exception: pass
                    MIGRATION_STATE={'phase':'error','percent':0,'message':str(e),'counts':{}}
        threading.Thread(target=worker,daemon=True).start()
        return jsonify(ok=True,started=True)

    app.register_blueprint(bp5)