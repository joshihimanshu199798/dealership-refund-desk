import os, sqlite3, uuid, smtplib, logging
from datetime import datetime
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DATABASE_PATH', BASE / 'refund_desk.db'))
UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', BASE / 'uploads'))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED = {'pdf','png','jpg','jpeg','webp','doc','docx','xls','xlsx','eml','txt'}
MAX_BYTES = 10 * 1024 * 1024
app = Flask(__name__)
# Production must provide a secret key; never silently use a predictable fallback.
_secret = os.getenv('SECRET_KEY')
if not _secret and os.getenv('FLASK_ENV') == 'production':
    raise RuntimeError('SECRET_KEY must be set in production.')
app.secret_key = _secret or 'local-development-only-not-for-deployment'
app.config.update(
    MAX_CONTENT_LENGTH=MAX_BYTES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', 'false').lower() in ('1','true','yes'),
    PERMANENT_SESSION_LIFETIME=__import__('datetime').timedelta(hours=8),
)
STATUSES = ['Submitted','Under Review','Documents Required','Approved for Payment','Payment Processed','Closed','Rejected']

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def now(): return datetime.now().astimezone().isoformat(timespec='seconds')

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('sales','accounts','admin')));
        CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, request_no TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), assigned_to TEXT, customer_name TEXT NOT NULL, customer_phone TEXT, customer_email TEXT, booking_no TEXT, vehicle_model TEXT, vin TEXT, refund_type TEXT NOT NULL, requested_amount REAL NOT NULL, reason TEXT NOT NULL, bank_name TEXT, account_last4 TEXT, status TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'Normal', payment_date TEXT, utr TEXT, processed_amount REAL, closure_note TEXT);
        CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'Internal', body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE, uploaded_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL, category TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notification_log(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, created_at TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL);
        """)
        # Demo credentials are seeded only when explicitly opted in for local testing.
        # Never create known default accounts automatically on a public deployment.
        if os.getenv('DEMO_MODE', 'false').lower() in ('1','true','yes') and con.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            demos=[('Demo Sales Executive','sales@example.com','SalesDemo123!','sales'),('Demo Accounts User','accounts@example.com','AccountsDemo123!','accounts'),('Demo Administrator','admin@example.com','AdminDemo123!','admin')]
            con.executemany('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',[(n,e,generate_password_hash(p),r) for n,e,p,r in demos])

def email_configured(): return bool(os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM'))

def send_notification(recipients, subject, body, request_id=None):
    recipients=sorted({x.strip() for x in recipients if x and x.strip()})
    if not recipients: return False
    outcome,detail='skipped','Email not configured; set SMTP_HOST and SMTP_FROM.'
    if email_configured():
        msg=EmailMessage(); msg['Subject']=subject; msg['From']=os.getenv('SMTP_FROM'); msg['To']=', '.join(recipients); msg.set_content(body)
        try:
            host=os.getenv('SMTP_HOST'); port=int(os.getenv('SMTP_PORT','587')); timeout=int(os.getenv('SMTP_TIMEOUT','15'))
            if os.getenv('SMTP_USE_SSL','false').lower() in ('1','true','yes'):
                with smtplib.SMTP_SSL(host,port,timeout=timeout) as server:
                    if os.getenv('SMTP_USERNAME'): server.login(os.getenv('SMTP_USERNAME'),os.getenv('SMTP_PASSWORD',''))
                    server.send_message(msg)
            else:
                with smtplib.SMTP(host,port,timeout=timeout) as server:
                    server.ehlo()
                    if os.getenv('SMTP_STARTTLS','true').lower() in ('1','true','yes'): server.starttls(); server.ehlo()
                    if os.getenv('SMTP_USERNAME'): server.login(os.getenv('SMTP_USERNAME'),os.getenv('SMTP_PASSWORD',''))
                    server.send_message(msg)
            outcome,detail='sent','SMTP server accepted the message.'
        except Exception as exc:
            outcome,detail='failed',f'{type(exc).__name__}: {exc}'; logging.exception('Refund Desk notification email failed')
    with db() as con:
        for recipient in recipients:
            con.execute('INSERT INTO notification_log(request_id,recipient,subject,created_at,outcome,detail) VALUES(?,?,?,?,?,?)',(request_id,recipient,subject,now(),outcome,detail))
    return outcome=='sent'

def sales_recipients(rid):
    with db() as con:
        row=con.execute('SELECT u.email FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
    return [row['email']] if row else []

def accounts_recipients():
    configured=[x.strip() for x in os.getenv('ACCOUNTS_NOTIFICATION_EMAILS','').split(',') if x.strip()]
    if configured: return configured
    with db() as con: return [r['email'] for r in con.execute("SELECT email FROM users WHERE role IN ('accounts','admin')").fetchall()]

def request_summary(r):
    return f"Request: {r['request_no']}\nCustomer: {r['customer_name']}\nRefund type: {r['refund_type']}\nRequested amount: INR {r['requested_amount']:,.2f}\nBooking/order: {r['booking_no'] or 'Not provided'}\nVehicle: {r['vehicle_model'] or 'Not provided'}\nCurrent status: {r['status']}\nReason: {r['reason']}\n"

def log_action(con,rid,action,details):
    con.execute('INSERT INTO audit_log(request_id,user_id,created_at,action,details) VALUES(?,?,?,?,?)',(rid,session['user_id'],now(),action,details))

def login_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return fn(*args,**kwargs)
    return wrapper

def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args,**kwargs):
            if 'user_id' not in session: return redirect(url_for('login'))
            if session.get('role') not in roles: abort(403)
            return fn(*args,**kwargs)
        return wrapper
    return deco

def save_uploads(files, rid, category='Supporting document'):
    saved=[]
    for f in files:
        if not f or not f.filename: continue
        original=secure_filename(f.filename); ext=original.rsplit('.',1)[-1].lower() if '.' in original else ''
        if ext not in ALLOWED: raise ValueError(f'File type not allowed: {original}')
        stored=f'{uuid.uuid4().hex}.{ext}'; f.save(UPLOAD_DIR/stored); saved.append((original,stored,category))
    return saved

def add_attachments(con,rid,saved):
    for original,stored,category in saved:
        con.execute('INSERT INTO attachments(request_id,uploaded_by,created_at,original_name,stored_name,category) VALUES(?,?,?,?,?,?)',(rid,session['user_id'],now(),original,stored,category))

@app.context_processor
def globals_for_templates(): return {'STATUSES':STATUSES}

@app.cli.command('create-user')
def create_user_command():
    """Create a named application user interactively; run only in a trusted environment."""
    import click
    name=click.prompt('Full name')
    email=click.prompt('Email').strip().lower()
    role=click.prompt('Role',type=click.Choice(['sales','accounts','admin'],case_sensitive=False)).lower()
    password=click.prompt('Password',hide_input=True,confirmation_prompt=True)
    if len(password)<12:
        raise click.ClickException('Password must be at least 12 characters.')
    try:
        with db() as con:
            con.execute('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',
                        (name,email,generate_password_hash(password),role))
    except sqlite3.IntegrityError:
        raise click.ClickException('That email already exists.')
    click.echo(f'Created {role} user: {email}')

@app.route('/healthz')
def healthz(): return {'status':'ok'},200

@app.route('/')
def home(): return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        with db() as con: user=con.execute('SELECT * FROM users WHERE lower(email)=?',(email,)).fetchone()
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session.update(user_id=user['id'],name=user['name'],role=user['role'],email=user['email']); session.permanent = True
            return redirect(url_for('dashboard'))
        flash('Email or password is incorrect.','error')
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    q=request.args.get('q','').strip(); status=request.args.get('status','').strip()
    sql='SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE 1=1'; args=[]
    if session['role']=='sales': sql+=' AND r.created_by=?'; args.append(session['user_id'])
    if q: sql+=' AND (r.request_no LIKE ? OR r.customer_name LIKE ? OR r.booking_no LIKE ? OR r.utr LIKE ?)'; args += [f'%{q}%']*4
    if status and status in STATUSES: sql+=' AND r.status=?'; args.append(status)
    sql+=' ORDER BY CASE r.priority WHEN "Urgent" THEN 0 WHEN "High" THEN 1 ELSE 2 END, r.created_at DESC'
    with db() as con:
        rows=con.execute(sql,args).fetchall()
        counts={s:con.execute('SELECT COUNT(*) FROM requests'+(' WHERE created_by=? AND status=?' if session['role']=='sales' else ' WHERE status=?'),((session['user_id'],s) if session['role']=='sales' else (s,))).fetchone()[0] for s in STATUSES}
    return render_template('dashboard.html',requests=rows,counts=counts,q=q,selected_status=status)

@app.route('/requests/new',methods=['GET','POST'])
@roles_required('sales','admin')
def new_request():
    if request.method=='POST':
        try:
            amount=float(request.form.get('requested_amount','0'))
            if amount<=0: raise ValueError('Refund amount must be greater than zero.')
            if any(not request.form.get(k,'').strip() for k in ['customer_name','refund_type','reason']): raise ValueError('Please complete all required fields.')
            created=now()
            with db() as con:
                cur=con.execute('''INSERT INTO requests(request_no,created_at,updated_at,created_by,customer_name,customer_phone,customer_email,booking_no,vehicle_model,vin,refund_type,requested_amount,reason,bank_name,account_last4,status,priority) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',('RF-'+datetime.now().strftime('%y%m')+'-'+uuid.uuid4().hex[:6].upper(),created,created,session['user_id'],request.form['customer_name'].strip(),request.form.get('customer_phone','').strip(),request.form.get('customer_email','').strip(),request.form.get('booking_no','').strip(),request.form.get('vehicle_model','').strip(),request.form.get('vin','').strip(),request.form['refund_type'],amount,request.form['reason'].strip(),request.form.get('bank_name','').strip(),request.form.get('account_last4','').strip(),'Submitted',request.form.get('priority','Normal')))
                rid=cur.lastrowid; add_attachments(con,rid,save_uploads(request.files.getlist('attachments'),rid,'Initial supporting document')); log_action(con,rid,'Request submitted','Refund request created by sales.')
            with db() as con: row=con.execute('SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
            send_notification(accounts_recipients(),f"New refund request {row['request_no']}",f"A new refund request was submitted by {row['requester']}.\n\n{request_summary(row)}\nOpen the Refund Desk to review documents and update the request.",rid)
            flash('Refund request submitted successfully.','success'); return redirect(url_for('request_detail',rid=rid))
        except (ValueError,sqlite3.Error) as e: flash(str(e),'error')
    return render_template('new_request.html')

@app.route('/requests/<int:rid>')
@login_required
def request_detail(rid):
    with db() as con:
        r=con.execute('SELECT r.*,u.name AS requester,u.email AS requester_email FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
        if not r: abort(404)
        if session['role']=='sales' and r['created_by']!=session['user_id']: abort(403)
        comments=con.execute('SELECT c.*,u.name AS author,u.role FROM comments c JOIN users u ON u.id=c.user_id WHERE request_id=? ORDER BY c.created_at',(rid,)).fetchall()
        attachments=con.execute('SELECT a.*,u.name AS uploader FROM attachments a JOIN users u ON u.id=a.uploaded_by WHERE request_id=? ORDER BY a.created_at DESC',(rid,)).fetchall()
        audit=con.execute('SELECT a.*,u.name AS actor FROM audit_log a JOIN users u ON u.id=a.user_id WHERE request_id=? ORDER BY a.created_at DESC',(rid,)).fetchall() if session['role']!='sales' else []
    return render_template('detail.html',r=r,comments=comments,attachments=attachments,audit=audit)

@app.route('/requests/<int:rid>/comment',methods=['POST'])
@login_required
def add_comment(rid):
    body=request.form.get('body','').strip(); visibility=request.form.get('visibility','Internal'); notification=None
    with db() as con:
        r=con.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not r: abort(404)
        if session['role']=='sales' and r['created_by']!=session['user_id']: abort(403)
        if not body: flash('Comment cannot be empty.','error')
        else:
            if session['role']=='sales': visibility='Internal'
            con.execute('INSERT INTO comments(request_id,user_id,created_at,visibility,body) VALUES(?,?,?,?,?)',(rid,session['user_id'],now(),visibility,body))
            con.execute('UPDATE requests SET updated_at=? WHERE id=?',(now(),rid)); log_action(con,rid,'Comment added',f'{visibility} comment added.')
            if visibility=='Sales visible' and session['role'] in ('accounts','admin'):
                notification=(f"Refund update: {r['request_no']}",f"Accounts added an update to {r['request_no']}.\n\n{body}\n\n{request_summary(r)}")
    if notification: send_notification(sales_recipients(rid),notification[0],notification[1],rid)
    return redirect(url_for('request_detail',rid=rid))

@app.route('/requests/<int:rid>/update',methods=['POST'])
@roles_required('accounts','admin')
def update_request(rid):
    status=request.form.get('status',''); assigned=request.form.get('assigned_to','').strip(); priority=request.form.get('priority','Normal')
    if status not in STATUSES: flash('Invalid status.','error'); return redirect(url_for('request_detail',rid=rid))
    utr=request.form.get('utr','').strip(); paydate=request.form.get('payment_date','').strip(); closure=request.form.get('closure_note','').strip()
    try: processed=float(request.form['processed_amount']) if request.form.get('processed_amount','').strip() else None
    except ValueError: flash('Processed amount must be numeric.','error'); return redirect(url_for('request_detail',rid=rid))
    if status=='Payment Processed' and (not utr or not paydate or not processed or processed<=0):
        flash('To mark Payment Processed, enter payment date, UTR/reference and processed amount.','error'); return redirect(url_for('request_detail',rid=rid))
    with db() as con:
        r=con.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not r: abort(404)
        con.execute('UPDATE requests SET status=?,assigned_to=?,priority=?,utr=?,payment_date=?,processed_amount=?,closure_note=?,updated_at=? WHERE id=?',(status,assigned,priority,utr or None,paydate or None,processed,closure or None,now(),rid))
        log_action(con,rid,'Request updated',f'Status: {r["status"]} → {status}; priority: {priority}.')
        updated=con.execute('SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
    if status=='Payment Processed':
        extra=f"\nProcessed amount: INR {processed:,.2f}\nPayment date: {paydate}\nUTR/reference: {utr}\nClosure note: {closure or '—'}"
        send_notification(sales_recipients(rid),f"Refund processed: {updated['request_no']}",f"Accounts marked the refund as processed. Please share the UTR with the customer as per dealership procedure.\n{extra}\n\n{request_summary(updated)}",rid)
    elif status=='Documents Required':
        send_notification(sales_recipients(rid),f"Documents required: {updated['request_no']}",f"Accounts has requested additional documents for this refund. Please open the Refund Desk and check the comments/documents section.\n\n{request_summary(updated)}",rid)
    else:
        send_notification(sales_recipients(rid),f"Refund status updated: {updated['request_no']}",f"Accounts updated the status to: {status}.\n\n{request_summary(updated)}",rid)
    flash('Request updated. Email sent if SMTP is configured; delivery status is recorded in notification logs.','success')
    return redirect(url_for('request_detail',rid=rid))

@app.route('/requests/<int:rid>/upload',methods=['POST'])
@login_required
def upload_more(rid):
    with db() as con:
        r=con.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not r: abort(404)
        if session['role']=='sales' and r['created_by']!=session['user_id']: abort(403)
        try:
            saved=save_uploads(request.files.getlist('attachments'),rid,request.form.get('category','Additional document')); add_attachments(con,rid,saved)
            if saved: log_action(con,rid,'Attachment uploaded',f'{len(saved)} file(s) added.')
            flash(f'{len(saved)} file(s) uploaded.','success')
        except ValueError as e: flash(str(e),'error')
    return redirect(url_for('request_detail',rid=rid))

@app.route('/files/<int:aid>')
@login_required
def download_attachment(aid):
    with db() as con:
        a=con.execute('SELECT a.*,r.created_by FROM attachments a JOIN requests r ON r.id=a.request_id WHERE a.id=?',(aid,)).fetchone()
        if not a: abort(404)
        if session['role']=='sales' and a['created_by']!=session['user_id']: abort(403)
    return send_from_directory(UPLOAD_DIR,a['stored_name'],as_attachment=True,download_name=a['original_name'])

@app.errorhandler(413)
def too_large(_): return 'File too large. Maximum upload size is 10 MB per request.',413

init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=os.getenv('FLASK_DEBUG')=='1')
