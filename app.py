import os, sqlite3, uuid, smtplib, logging, csv
from datetime import datetime
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from io import BytesIO, StringIO
from flask import send_file, Response
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DATABASE_PATH', BASE / 'refund_desk.db'))
UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', BASE / 'uploads'))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SUPABASE_URL = os.getenv('SUPABASE_URL', '').strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '').strip()
SUPABASE_STORAGE_BUCKET = os.getenv('SUPABASE_STORAGE_BUCKET', 'refund-attachments').strip()
_supabase = None
if os.getenv('FLASK_ENV') == 'production':
    if not os.getenv('DATABASE_URL', '').strip():
        raise RuntimeError('DATABASE_URL must point to a persistent PostgreSQL database in production.')
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for private production attachment storage.')

def storage_client():
    """Return the configured Supabase service client, or None for local development."""
    global _supabase
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None
    if _supabase is None:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _supabase
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
STATUSES = ['Pending CEO Approval','CEO Approved','Submitted','Under Review','Documents Required','Approved for Payment','Payment Processed','Closed','Rejected']

class PostgresConnection:
    """Small DB-API compatibility layer for the existing parameterized SQL."""
    def __init__(self, connection):
        self.connection = connection
    def __enter__(self):
        self.connection.__enter__()
        return self
    def __exit__(self, exc_type, exc, tb):
        return self.connection.__exit__(exc_type, exc, tb)
    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace('?', '%s'), params)
    def executemany(self, sql, params):
        return self.connection.executemany(sql.replace('?', '%s'), params)
    def close(self):
        return self.connection.close()

def db():
    database_url = os.getenv('DATABASE_URL', '').strip()
    if database_url:
        import psycopg
        from psycopg.rows import dict_row
        return PostgresConnection(psycopg.connect(database_url, row_factory=dict_row))
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def format_inr(value, decimals=2):
    """Format numeric values with Indian digit grouping, e.g. ₹12,34,567.89."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    decimals = max(0, min(4, int(decimals)))
    rendered = f"{abs(number):.{decimals}f}"
    whole, dot, fraction = rendered.partition('.')
    if len(whole) > 3:
        last_three = whole[-3:]
        leading = whole[:-3]
        groups = []
        while leading:
            groups.insert(0, leading[-2:])
            leading = leading[:-2]
        whole = ','.join(groups + [last_three])
    sign = '-' if number < 0 else ''
    return f"{sign}{whole}{dot}{fraction if dot else ''}"

@app.template_filter('inr')
def inr_filter(value, decimals=2):
    return format_inr(value, decimals)

def now(): return datetime.now().astimezone().isoformat(timespec='seconds')

def init_db():
    postgres = bool(os.getenv('DATABASE_URL', '').strip())
    if postgres:
        schema = """
        CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('sales','cashier','accounts','admin','ceo')));
        CREATE TABLE IF NOT EXISTS requests(id BIGSERIAL PRIMARY KEY, request_no TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by BIGINT NOT NULL REFERENCES users(id), assigned_to TEXT, customer_name TEXT NOT NULL, customer_code TEXT, request_location TEXT, customer_phone TEXT, customer_email TEXT, booking_no TEXT, vehicle_model TEXT, vin TEXT, refund_type TEXT NOT NULL, requested_amount DOUBLE PRECISION NOT NULL, reason TEXT NOT NULL, bank_name TEXT, account_no TEXT, ifsc_code TEXT, account_last4 TEXT, status TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'Normal', payment_date TEXT, utr TEXT, processed_amount DOUBLE PRECISION, closure_note TEXT);
        CREATE TABLE IF NOT EXISTS comments(id BIGSERIAL PRIMARY KEY, request_id BIGINT NOT NULL REFERENCES requests(id) ON DELETE CASCADE, user_id BIGINT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'Internal', body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attachments(id BIGSERIAL PRIMARY KEY, request_id BIGINT NOT NULL REFERENCES requests(id) ON DELETE CASCADE, uploaded_by BIGINT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL, category TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log(id BIGSERIAL PRIMARY KEY, request_id BIGINT NOT NULL REFERENCES requests(id) ON DELETE CASCADE, user_id BIGINT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notification_log(id BIGSERIAL PRIMARY KEY, request_id BIGINT REFERENCES requests(id) ON DELETE SET NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, created_at TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL);
        """
        with db() as con:
            # Gunicorn can import this module in multiple workers at startup.
            # Serialize schema creation to prevent PostgreSQL catalog races.
            con.execute('SELECT pg_advisory_xact_lock(741928361)')
            for statement in schema.split(';'):
                if statement.strip():
                    con.execute(statement)
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS customer_code TEXT')
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS request_location TEXT')
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS account_no TEXT')
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS ifsc_code TEXT')
            con.execute('ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check')
            con.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK(role IN ('sales','cashier','accounts','admin','ceo'))")
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS ceo_approved INTEGER NOT NULL DEFAULT 0')
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS ceo_decision TEXT')
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS ceo_approved_by BIGINT REFERENCES users(id)')
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS ceo_approved_at TEXT')
            con.execute('ALTER TABLE requests ADD COLUMN IF NOT EXISTS ceo_comments TEXT')
    else:
        with db() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('sales','cashier','accounts','admin')));
            CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, request_no TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), assigned_to TEXT, customer_name TEXT NOT NULL, customer_phone TEXT, customer_email TEXT, booking_no TEXT, vehicle_model TEXT, vin TEXT, refund_type TEXT NOT NULL, requested_amount REAL NOT NULL, reason TEXT NOT NULL, bank_name TEXT, account_last4 TEXT, status TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'Normal', payment_date TEXT, utr TEXT, processed_amount REAL, closure_note TEXT);
            CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'Internal', body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE, uploaded_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL, category TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notification_log(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, created_at TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            columns = [row['name'] for row in con.execute('PRAGMA table_info(requests)').fetchall()]
            if 'customer_code' not in columns:
                con.execute('ALTER TABLE requests ADD COLUMN customer_code TEXT')
            if 'request_location' not in columns:
                con.execute('ALTER TABLE requests ADD COLUMN request_location TEXT')
            if 'account_no' not in columns:
                con.execute('ALTER TABLE requests ADD COLUMN account_no TEXT')
            if 'ifsc_code' not in columns:
                con.execute('ALTER TABLE requests ADD COLUMN ifsc_code TEXT')
        raw = sqlite3.connect(DB_PATH)
        try:
            raw.execute('PRAGMA foreign_keys=OFF')
            schema_row = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            if schema_row and "'ceo'" not in (schema_row[0] or ''):
                raw.execute("CREATE TABLE users_new(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('sales','cashier','accounts','admin','ceo')))")
                raw.execute('INSERT INTO users_new(id,name,email,password_hash,role) SELECT id,name,email,password_hash,role FROM users')
                raw.execute('DROP TABLE users')
                raw.execute('ALTER TABLE users_new RENAME TO users')
            cols=[row[1] for row in raw.execute('PRAGMA table_info(requests)').fetchall()]
            for name, definition in [('ceo_approved','INTEGER NOT NULL DEFAULT 0'),('ceo_decision','TEXT'),('ceo_approved_by','INTEGER'),('ceo_approved_at','TEXT'),('ceo_comments','TEXT')]:
                if name not in cols:
                    raw.execute(f'ALTER TABLE requests ADD COLUMN {name} {definition}')
            raw.commit()
        finally:
            raw.execute('PRAGMA foreign_keys=ON')
            raw.close()
    with db() as con:
        user_count = con.execute('SELECT COUNT(*) AS count FROM users').fetchone()['count']
        if os.getenv('DEMO_MODE', 'false').lower() in ('1','true','yes') and user_count == 0:
            demos=[('Demo Sales Executive','sales@example.com','SalesDemo123!','sales'),('Demo Accounts User','accounts@example.com','AccountsDemo123!','accounts'),('Demo Administrator','admin@example.com','AdminDemo123!','admin'),('Demo CEO','ceo@example.com','CEOdemo123!!','ceo')]
            con.executemany('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',[(n,e,generate_password_hash(p),r) for n,e,p,r in demos])
        # One-time administrator bootstrap for hosted environments without shell access.
        # Set both variables temporarily, deploy once, then remove both variables.
        admin_email = os.getenv('BOOTSTRAP_ADMIN_EMAIL', '').strip().lower()
        admin_password = os.getenv('BOOTSTRAP_ADMIN_PASSWORD', '')
        if admin_email and admin_password and len(admin_password) >= 8:
            exists = con.execute('SELECT id FROM users WHERE lower(email)=?', (admin_email,)).fetchone()
            if not exists:
                con.execute('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',
                            ('Refund Desk Administrator', admin_email, generate_password_hash(admin_password), 'admin'))

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

def ceo_recipients():
    configured=[x.strip() for x in os.getenv('CEO_NOTIFICATION_EMAILS','').split(',') if x.strip()]
    if configured: return configured
    with db() as con: return [r['email'] for r in con.execute("SELECT email FROM users WHERE role='ceo'").fetchall()]

def accounts_recipients():
    configured=[x.strip() for x in os.getenv('ACCOUNTS_NOTIFICATION_EMAILS','').split(',') if x.strip()]
    if configured: return configured
    with db() as con: return [r['email'] for r in con.execute("SELECT email FROM users WHERE role IN ('cashier','accounts','admin')").fetchall()]

def request_summary(r):
    return f"Request: {r['request_no']}\\nCustomer: {r['customer_name']}\\nCustomer code: {r['customer_code'] or 'Not provided'}\\nSales location: {r['request_location'] or 'Not provided'}\\nRefund type: {r['refund_type']}\\nRequested amount: INR {r['requested_amount']:,.2f}\\nBank: {r['bank_name'] or 'Not provided'}\\nAccount number: {r['account_no'] or 'Not provided'}\\nIFSC: {r['ifsc_code'] or 'Not provided'}\\nCurrent status: {r['status']}\\nReason: {r['reason']}\\n"
def parse_dt(value):
    if not value: return None
    try: return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError): return None

def request_age_days(row):
    created = parse_dt(row['created_at'])
    if not created: return 0
    current = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    return max(0, (current - created).days)

def is_open_status(status):
    return status not in ('Payment Processed', 'Closed', 'Rejected')

def duplicate_candidates(form):
    code=form.get('customer_code','').strip()
    name=form.get('customer_name','').strip()
    refund_type=form.get('refund_type','').strip()
    try: amount=float(form.get('requested_amount','0') or 0)
    except ValueError: return []
    if not name or not refund_type or amount<=0: return []
    with db() as con:
        if code:
            rows=con.execute("SELECT id,request_no,created_at,customer_name,customer_code,request_location,refund_type,requested_amount,status FROM requests WHERE (lower(customer_code)=lower(?) OR (lower(customer_name)=lower(?) AND refund_type=? AND ABS(requested_amount-?)<=1.00)) AND status NOT IN ('Rejected','Closed') ORDER BY created_at DESC LIMIT 6",(code,name,refund_type,amount)).fetchall()
        else:
            rows=con.execute("SELECT id,request_no,created_at,customer_name,customer_code,request_location,refund_type,requested_amount,status FROM requests WHERE lower(customer_name)=lower(?) AND refund_type=? AND ABS(requested_amount-?)<=1.00 AND status NOT IN ('Rejected','Closed') ORDER BY created_at DESC LIMIT 6",(name,refund_type,amount)).fetchall()
    return list(rows)

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
        stored=f'{uuid.uuid4().hex}.{ext}'
        client = storage_client()
        if client:
            content_type = f.mimetype or 'application/octet-stream'
            client.storage.from_(SUPABASE_STORAGE_BUCKET).upload(
                stored, f.read(), {'content-type': content_type, 'upsert': 'false'}
            )
        else:
            f.save(UPLOAD_DIR/stored)
        saved.append((original,stored,category))
    return saved

def add_attachments(con,rid,saved):
    for original,stored,category in saved:
        con.execute('INSERT INTO attachments(request_id,uploaded_by,created_at,original_name,stored_name,category) VALUES(?,?,?,?,?,?)',(rid,session['user_id'],now(),original,stored,category))

@app.context_processor
def globals_for_templates(): return {'STATUSES':STATUSES, 'demo_mode': os.getenv('DEMO_MODE', 'false').lower() in ('1','true','yes')}

@app.cli.command('create-user')
def create_user_command():
    """Create a named application user interactively; run only in a trusted environment."""
    import click
    name=click.prompt('Full name')
    email=click.prompt('Email').strip().lower()
    role=click.prompt('Role',type=click.Choice(['sales','accounts','admin','ceo'],case_sensitive=False)).lower()
    password=click.prompt('Password',hide_input=True,confirmation_prompt=True)
    if len(password)<8:
        raise click.ClickException('Password must be at least 8 characters.')
    try:
        with db() as con:
            con.execute('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',
                        (name,email,generate_password_hash(password),role))
    except Exception as exc:
        # psycopg raises UniqueViolation for PostgreSQL; sqlite raises IntegrityError.
        if isinstance(exc, sqlite3.IntegrityError) or exc.__class__.__name__ in ('UniqueViolation', 'IntegrityError'):
            raise click.ClickException('That email already exists.')
        raise
    click.echo(f'Created {role} user: {email}')

@app.route('/healthz')
def healthz():
    # Report healthy only when the configured database is reachable.
    try:
        with db() as con:
            con.execute('SELECT 1').fetchone()
        return {'status':'ok','database':'ok'},200
    except Exception:
        logging.exception('Health check database probe failed')
        return {'status':'unavailable','database':'error'},503

@app.route('/')
def home(): return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        login_role=request.form.get('login_role','')
        allowed_login_roles={'sales': {'sales'}, 'accounts': {'accounts','cashier'}, 'admin': {'admin'}, 'ceo': {'ceo'}}
        with db() as con: user=con.execute('SELECT * FROM users WHERE lower(email)=?',(email,)).fetchone()
        if user and check_password_hash(user['password_hash'],password) and user['role'] in allowed_login_roles.get(login_role,set()):
            effective_role = 'accounts' if user['role'] == 'cashier' else user['role']
            session.clear(); session.update(user_id=user['id'],name=user['name'],role=effective_role,email=user['email']); session.permanent = True
            return redirect(url_for('dashboard'))
        flash('Email, password, or selected login type is incorrect. Choose the login option assigned to your account.','error')
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    view=request.args.get('view','overview').strip().lower()
    if view not in ('overview','requests','analytics'): view='overview'
    q=request.args.get('q','').strip()
    status=request.args.get('status','').strip()
    start_date=request.args.get('start_date','').strip()
    end_date=request.args.get('end_date','').strip()
    for date_value in (start_date,end_date):
        if date_value and (len(date_value)!=10 or parse_dt(date_value) is None or parse_dt(date_value).date().isoformat()!=date_value):
            start_date=end_date=''
            break
    if start_date and end_date and start_date>end_date:
        start_date=end_date=''
    sort=request.args.get('sort','newest').strip().lower()
    if sort not in ('newest','oldest'):
        sort='newest'
    sql='SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE 1=1'
    args=[]
    if session['role']=='sales':
        sql+=' AND r.created_by=?'
        args.append(session['user_id'])
    # CEO sees every refund, including historical and Accounts-stage requests.
    elif session['role']=='accounts':
        # Accounts can see both queues; processing remains gated on CEO approval in detail/update routes.
        pass
    if q:
        sql+=' AND (r.request_no LIKE ? OR r.customer_name LIKE ? OR r.customer_code LIKE ? OR r.request_location LIKE ? OR r.utr LIKE ? OR r.booking_no LIKE ? OR r.customer_phone LIKE ? OR r.customer_email LIKE ? OR r.vehicle_model LIKE ? OR r.vin LIKE ?)'
        args += [f'%{q}%']*10
    if start_date:
        sql+=' AND substr(r.created_at,1,10)>=?'; args.append(start_date)
    if end_date:
        sql+=' AND substr(r.created_at,1,10)<=?'; args.append(end_date)
    approval_filter=request.args.get('approval','all').strip().lower()
    if approval_filter not in ('all','approved','pending'): approval_filter='all'
    if session.get('role')=='accounts':
        if approval_filter=='approved':
            sql+=' AND COALESCE(r.ceo_approved,0)=1'
        elif approval_filter=='pending':
            sql+=" AND COALESCE(r.ceo_approved,0)=0 AND r.status NOT IN ('Rejected','Closed','Payment Processed')"
    if status and status in STATUSES:
        sql+=' AND r.status=?'
        args.append(status)
    sql += " ORDER BY CASE WHEN lower(r.priority)='super duper important' THEN 0 WHEN lower(r.priority)='urgent' THEN 1 WHEN lower(r.priority)='high' THEN 2 ELSE 3 END, created_at " + ("ASC" if sort=='oldest' else "DESC") + ", id " + ("ASC" if sort=='oldest' else "DESC")
    role=session.get('role')
    scope_sql="SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE 1=1"
    scope_args=[]
    if role=='sales':
        scope_sql+=' AND r.created_by=?'; scope_args.append(session['user_id'])
    elif role=='accounts':
        # Separate approved and awaiting-CEO queues are exposed through the dashboard filter.
        pass
    with db() as con:
        rows=con.execute(sql,args).fetchall()
        all_rows=con.execute(scope_sql,scope_args).fetchall()
    sla_days=max(1,int(os.getenv('REFUND_SLA_DAYS','3')))
    visible=[]; overdue_count=0
    for row in rows:
        age=request_age_days(row)
        overdue=is_open_status(row['status']) and age>=sla_days
        overdue_count += int(overdue)
        item=dict(row); item['age_days']=age; item['is_overdue']=overdue
        if request.args.get('overdue')=='1' and not overdue: continue
        visible.append(item)
    counts={s:sum(1 for r in all_rows if r['status']==s) for s in STATUSES}
    all_ages=[request_age_days(r) for r in all_rows]
    dashboard_stats={
        'total':len(all_rows),
        'total_value':sum(float(r['requested_amount'] or 0) for r in all_rows),
        'pending_ceo':sum(1 for r in all_rows if not r['ceo_approved'] and r['status'] not in ('Rejected','Closed','Payment Processed')),
        'docs_required':sum(1 for r in all_rows if r['status']=='Documents Required'),
        'overdue':sum(1 for r in all_rows if is_open_status(r['status']) and request_age_days(r)>=sla_days),
        'processed':sum(1 for r in all_rows if r['status'] in ('Payment Processed','Closed')),
        'processed_value':sum(float(r['processed_amount'] or 0) for r in all_rows if r['status'] in ('Payment Processed','Closed')),
        'unassigned':sum(1 for r in all_rows if not (r['assigned_to'] or '').strip() and r['status'] not in ('Payment Processed','Closed','Rejected')),
        'avg_age':round(sum(all_ages)/len(all_ages),1) if all_ages else 0,
        'awaiting_accounts':sum(1 for r in all_rows if r['ceo_approved'] and r['status'] not in ('Payment Processed','Closed','Rejected')),
    }
    visible_value=sum(float(r['requested_amount'] or 0) for r in visible)
    # Smart control-centre alerts are built only from the user's permitted portfolio.
    priority_alerts=[]
    high_value_threshold=float(os.getenv('HIGH_VALUE_REFUND_THRESHOLD','100000'))
    for r in all_rows:
        age=request_age_days(r)
        if not is_open_status(r['status']):
            continue
        if not r['ceo_approved'] and r['status'] not in ('Rejected','Closed','Payment Processed'):
            priority_alerts.append({'title':'CEO decision required','detail':f"{r['request_no']} · {r['customer_name']}",'amount':float(r['requested_amount'] or 0),'age':age,'priority':0,'url':url_for('dashboard',view='requests',status='Pending CEO Approval')})
        if r['status']=='Documents Required':
            priority_alerts.append({'title':'Missing documents','detail':f"{r['request_no']} · {r['customer_name']}",'amount':float(r['requested_amount'] or 0),'age':age,'priority':1,'url':url_for('dashboard',view='requests',status='Documents Required')})
        if age>=sla_days * 2:
            priority_alerts.append({'title':'Critical SLA escalation','detail':f"{r['request_no']} · {r['customer_name']} · {age} days open",'amount':float(r['requested_amount'] or 0),'age':age,'priority':1,'url':url_for('dashboard',view='requests',overdue=1)})
        elif age>=sla_days:
            priority_alerts.append({'title':'SLA overdue','detail':f"{r['request_no']} · {r['customer_name']} · {age} days open",'amount':float(r['requested_amount'] or 0),'age':age,'priority':2,'url':url_for('dashboard',view='requests',overdue=1)})
        elif age>=max(1,sla_days-1):
            priority_alerts.append({'title':'SLA due soon','detail':f"{r['request_no']} · {r['customer_name']} · {age} days open",'amount':float(r['requested_amount'] or 0),'age':age,'priority':3,'url':url_for('dashboard',view='requests',status=r['status'])})
        if float(r['requested_amount'] or 0)>=high_value_threshold:
            priority_alerts.append({'title':'High-value refund','detail':f"{r['request_no']} · {r['customer_name']}",'amount':float(r['requested_amount'] or 0),'age':age,'priority':3,'url':url_for('request_detail',rid=r['id'])})
    priority_alerts.sort(key=lambda item:(item['priority'],-item['age'],-item['amount']))
    priority_alerts=priority_alerts[:8]
    from collections import defaultdict
    from datetime import date
    month_totals=defaultdict(float); month_counts=defaultdict(int); branch_totals=defaultdict(float); branch_counts=defaultdict(int)
    today=date.today(); month_keys=[]
    for offset in range(5,-1,-1):
        year=today.year; month=today.month-offset
        while month<=0:
            month+=12; year-=1
        key=f'{year:04d}-{month:02d}'; month_keys.append(key); month_totals[key]=0.0; month_counts[key]=0
    for r in all_rows:
        created=parse_dt(r['created_at'])
        key=created.strftime('%Y-%m') if created else ''
        if key in month_totals:
            month_totals[key]+=float(r['requested_amount'] or 0); month_counts[key]+=1
        branch=(r['request_location'] or '').strip() or 'Unspecified location'
        branch_totals[branch]+=float(r['requested_amount'] or 0); branch_counts[branch]+=1
    analytics_months=[{'label':key,'count':month_counts[key],'value':month_totals[key]} for key in month_keys]
    analytics_branches=[{'name':name,'count':branch_counts[name],'value':branch_totals[name]} for name in sorted(branch_totals,key=lambda n:(-branch_totals[n],n.lower()))[:8]]
    analytics_statuses=[{'name':s,'count':counts[s]} for s in STATUSES if counts[s]]
    analytics={'requested_total':dashboard_stats['total_value'],'processed_total':dashboard_stats['processed_value'],'open_count':sum(1 for r in all_rows if is_open_status(r['status'])),'avg_age':dashboard_stats['avg_age'],'months':analytics_months,'branches':analytics_branches,'statuses':analytics_statuses}
    return render_template('dashboard.html',requests=visible,counts=counts,dashboard_stats=dashboard_stats,q=q,selected_status=status,sort=sort,approval_filter=approval_filter,overdue_count=overdue_count,overdue_only=request.args.get('overdue')=='1',sla_days=sla_days,view=view,start_date=start_date,end_date=end_date,visible_value=visible_value,visible_count=len(visible),priority_alerts=priority_alerts,analytics=analytics,high_value_threshold=high_value_threshold)


@app.route('/executive')
@roles_required('ceo','admin')
def executive_control_centre():
    """Executive-only snapshot of approvals, financial exposure, ageing and branch risk."""
    from collections import defaultdict
    with db() as con:
        rows=con.execute("""SELECT r.*,u.name AS requester FROM requests r
                            JOIN users u ON u.id=r.created_by
                            ORDER BY r.created_at DESC""").fetchall()
    sla_days=max(1,int(os.getenv('REFUND_SLA_DAYS','3')))
    high_value_threshold=float(os.getenv('HIGH_VALUE_REFUND_THRESHOLD','100000'))
    open_rows=[r for r in rows if is_open_status(r['status'])]
    pending=[r for r in rows if not r['ceo_approved'] and r['status'] not in ('Rejected','Closed','Payment Processed')]
    overdue=[r for r in open_rows if request_age_days(r)>=sla_days]
    critical=[r for r in open_rows if request_age_days(r)>=sla_days*2]
    unassigned=[r for r in open_rows if not (r['assigned_to'] or '').strip()]
    branch_values=defaultdict(lambda:{'count':0,'value':0.0,'overdue':0})
    for r in open_rows:
        branch=(r['request_location'] or '').strip() or 'Unspecified location'
        branch_values[branch]['count']+=1
        branch_values[branch]['value']+=float(r['requested_amount'] or 0)
        branch_values[branch]['overdue']+=int(request_age_days(r)>=sla_days)
    branches=[{'name':name,**values} for name,values in branch_values.items()]
    branches.sort(key=lambda x:(-x['value'],x['name'].lower()))
    recent_pending=sorted(pending,key=lambda r:(-request_age_days(r),-float(r['requested_amount'] or 0)))[:10]
    critical_cases=sorted(critical,key=lambda r:(-request_age_days(r),-float(r['requested_amount'] or 0)))[:10]
    metrics={
        'open_count':len(open_rows),
        'open_value':sum(float(r['requested_amount'] or 0) for r in open_rows),
        'pending_count':len(pending),
        'pending_value':sum(float(r['requested_amount'] or 0) for r in pending),
        'overdue_count':len(overdue),
        'overdue_value':sum(float(r['requested_amount'] or 0) for r in overdue),
        'critical_count':len(critical),
        'critical_value':sum(float(r['requested_amount'] or 0) for r in critical),
        'unassigned_count':len(unassigned),
        'high_value_count':sum(1 for r in open_rows if float(r['requested_amount'] or 0)>=high_value_threshold),
        'processed_value':sum(float(r['processed_amount'] or 0) for r in rows if r['status'] in ('Payment Processed','Closed')),
    }
    return render_template('executive.html',metrics=metrics,branches=branches[:10],
                           pending=recent_pending,critical_cases=critical_cases,
                           sla_days=sla_days,high_value_threshold=high_value_threshold)


@app.route('/requests/ceo-bulk-approve', methods=['POST'])
@roles_required('ceo')
def ceo_bulk_approve():
    raw_ids=request.form.getlist('request_ids')
    ids=[]
    for value in raw_ids:
        try:
            rid=int(value)
            if rid>0 and rid not in ids: ids.append(rid)
        except (TypeError,ValueError):
            continue
    if not ids:
        flash('Select at least one pending refund to approve.','error')
        return redirect(url_for('dashboard',status='Pending CEO Approval'))
    stamp=now()
    approved_count=0
    skipped_count=0
    notify_rows=[]
    with db() as con:
        for rid in ids:
            row=con.execute('SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
            # Re-check eligibility server-side; the browser selection is not trusted.
            if not row or row['ceo_approved'] or row['status'] in ('Rejected','Closed','Payment Processed'):
                skipped_count+=1
                continue
            previous=row['ceo_decision'] or 'Pending'
            next_status='CEO Approved' if row['status'] in ('Pending CEO Approval','CEO Approved','Rejected','Submitted') else row['status']
            con.execute('UPDATE requests SET status=?,ceo_approved=1,ceo_decision=?,ceo_approved_by=?,ceo_approved_at=?,ceo_comments=NULL,updated_at=? WHERE id=?',
                        (next_status,'Approve',session['user_id'],stamp,stamp,rid))
            log_action(con,rid,'CEO decision' if previous=='Pending' else 'CEO decision changed',
                       f'{previous} → Approve by {session.get("name","CEO")} (bulk dashboard approval).')
            notify_rows.append(dict(row))
            approved_count+=1
    for row in notify_rows:
        send_notification(accounts_recipients(),f"CEO approved refund {row['request_no']}",
                          f"The CEO approved this refund from the dashboard bulk-approval action.\\n\\n{request_summary(row)}\\nAccounts may now review and process it.",row['id'])
    if approved_count:
        flash(f'Approved {approved_count} refund request(s). Each decision was recorded in its activity log.','success')
    if skipped_count:
        flash(f'Skipped {skipped_count} request(s) because they were no longer eligible for approval.','error')
    return redirect(url_for('dashboard',status='Pending CEO Approval'))


def csv_safe(value):
    """Prevent spreadsheet formula execution from untrusted exported text."""
    if value is None:
        return ''
    text_value=str(value)
    if text_value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + text_value
    return text_value

@app.route('/reports/refunds.csv')
@roles_required('cashier','accounts','admin')
def export_refunds_csv():
    """Export the current filtered refund queue for authorized finance staff."""
    q=request.args.get('q','').strip()
    status=request.args.get('status','').strip()
    start_date=request.args.get('start_date','').strip()
    end_date=request.args.get('end_date','').strip()
    for date_value in (start_date,end_date):
        if date_value and (len(date_value)!=10 or parse_dt(date_value) is None or parse_dt(date_value).date().isoformat()!=date_value):
            start_date=end_date=''
            break
    if start_date and end_date and start_date>end_date:
        start_date=end_date=''
    sort=request.args.get('sort','newest').strip().lower()
    if sort not in ('newest','oldest'): sort='newest'
    sql='SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE 1=1'
    args=[]
    if session['role']=='accounts':
        sql+=' AND COALESCE(r.ceo_approved,0)=1'
    if q:
        sql+=' AND (r.request_no LIKE ? OR r.customer_name LIKE ? OR r.customer_code LIKE ? OR r.request_location LIKE ? OR r.utr LIKE ? OR r.booking_no LIKE ? OR r.customer_phone LIKE ? OR r.customer_email LIKE ? OR r.vehicle_model LIKE ? OR r.vin LIKE ?)'
        args += [f'%{q}%']*10
    if status and status in STATUSES:
        sql+=' AND r.status=?'; args.append(status)
    if start_date:
        sql+=' AND substr(r.created_at,1,10)>=?'; args.append(start_date)
    if end_date:
        sql+=' AND substr(r.created_at,1,10)<=?'; args.append(end_date)
    sql += " ORDER BY r.created_at " + ("ASC" if sort=='oldest' else "DESC") + ", r.id " + ("ASC" if sort=='oldest' else "DESC")
    sla_days=max(1,int(os.getenv('REFUND_SLA_DAYS','3')))
    output=StringIO(newline='')
    writer=csv.writer(output)
    writer.writerow(['Request number','Created at','Last updated','Customer name','Customer code','Sales location','Refund type','Requested amount (INR)','Status','Priority','Assigned to','Age (days)','Overdue','Payment date','UTR / bank reference','Processed amount (INR)','Created by'])
    with db() as con:
        rows=con.execute(sql,args).fetchall()
    for row in rows:
        age=request_age_days(row)
        overdue=is_open_status(row['status']) and age>=sla_days
        if request.args.get('overdue')=='1' and not overdue: continue
        writer.writerow([csv_safe(row['request_no']),csv_safe(row['created_at']),csv_safe(row['updated_at']),csv_safe(row['customer_name']),csv_safe(row['customer_code'] or ''),csv_safe(row['request_location'] or ''),csv_safe(row['refund_type']),f"{row['requested_amount']:.2f}",csv_safe(row['status']),csv_safe(row['priority']),csv_safe(row['assigned_to'] or ''),age,'Yes' if overdue else 'No',csv_safe(row['payment_date'] or ''),csv_safe(row['utr'] or ''),f"{row['processed_amount']:.2f}" if row['processed_amount'] is not None else '',csv_safe(row['requester'])])
    response=Response('\ufeff'+output.getvalue(),mimetype='text/csv; charset=utf-8')
    response.headers['Content-Disposition']='attachment; filename=refund-queue.csv'
    response.headers['Cache-Control']='no-store'
    return response



@app.route('/audit')
@roles_required('admin','ceo')
def audit_trail():
    """Searchable cross-request audit trail for executive and administrator oversight."""
    q=request.args.get('q','').strip()[:120]
    action=request.args.get('action','').strip()[:80]
    start_date=request.args.get('start_date','').strip()
    end_date=request.args.get('end_date','').strip()
    for date_value in (start_date,end_date):
        if date_value and (len(date_value)!=10 or parse_dt(date_value) is None or parse_dt(date_value).date().isoformat()!=date_value):
            start_date=end_date=''
            break
    if start_date and end_date and start_date>end_date:
        start_date=end_date=''
    sql="""SELECT a.id,a.request_id,a.created_at,a.action,a.details,u.name AS actor,
                  r.request_no,r.customer_name,r.request_location
           FROM audit_log a JOIN users u ON u.id=a.user_id
           JOIN requests r ON r.id=a.request_id WHERE 1=1"""
    args=[]
    if q:
        sql+=" AND (r.request_no LIKE ? OR r.customer_name LIKE ? OR r.request_location LIKE ? OR u.name LIKE ? OR a.action LIKE ? OR a.details LIKE ?)"
        args += [f'%{q}%']*6
    if action:
        sql+=" AND a.action=?"; args.append(action)
    if start_date:
        sql+=" AND substr(a.created_at,1,10)>=?"; args.append(start_date)
    if end_date:
        sql+=" AND substr(a.created_at,1,10)<=?"; args.append(end_date)
    sql+=" ORDER BY a.created_at DESC,a.id DESC LIMIT 300"
    with db() as con:
        rows=con.execute(sql,args).fetchall()
        actions=[row['action'] for row in con.execute('SELECT DISTINCT action FROM audit_log ORDER BY action').fetchall()]
        total=con.execute('SELECT COUNT(*) AS count FROM audit_log').fetchone()['count']
    return render_template('audit.html',events=rows,actions=actions,q=q,selected_action=action,
                           start_date=start_date,end_date=end_date,total_events=total)



@app.route('/reconciliation')
@roles_required('cashier','accounts','admin')
def payment_reconciliation():
    """Review recorded payments for missing references, duplicates, and amount variance."""
    q=request.args.get('q','').strip()[:120]
    issue_filter=request.args.get('issue','all').strip().lower()
    if issue_filter not in ('all','matched','variance','duplicate','incomplete'):
        issue_filter='all'
    sql="""SELECT r.*,u.name AS requester FROM requests r
           JOIN users u ON u.id=r.created_by
           WHERE (r.status IN ('Payment Processed','Closed') OR r.payment_date IS NOT NULL
                  OR r.utr IS NOT NULL OR r.processed_amount IS NOT NULL)"""
    args=[]
    if q:
        sql+=" AND (r.request_no LIKE ? OR r.customer_name LIKE ? OR r.customer_code LIKE ? OR r.request_location LIKE ? OR r.utr LIKE ?)"
        args += [f'%{q}%']*5
    sql+=" ORDER BY COALESCE(r.payment_date,r.updated_at) DESC,r.id DESC"
    with db() as con:
        records=con.execute(sql,args).fetchall()
    utr_counts={}
    for row in records:
        reference=(row['utr'] or '').strip().casefold()
        if reference:
            utr_counts[reference]=utr_counts.get(reference,0)+1
    all_payments=[]
    for row in records:
        requested=float(row['requested_amount'] or 0)
        processed=float(row['processed_amount'] or 0)
        reference=(row['utr'] or '').strip()
        issues=[]
        if row['status'] in ('Payment Processed','Closed'):
            if not reference or not row['payment_date'] or row['processed_amount'] is None or processed<=0:
                issues.append('incomplete')
        if reference and utr_counts.get(reference.casefold(),0)>1:
            issues.append('duplicate')
        if row['processed_amount'] is not None and abs(processed-requested)>0.009:
            issues.append('variance')
        if not issues and row['status'] in ('Payment Processed','Closed') and reference and row['payment_date'] and row['processed_amount'] is not None and processed>0:
            issues.append('matched')
        item=dict(row)
        item.update(requested_value=requested,processed_value=processed,
                    variance=round(processed-requested,2),issues=issues,
                    issue_label='Duplicate UTR' if 'duplicate' in issues else ('Missing payment data' if 'incomplete' in issues else ('Amount variance' if 'variance' in issues else ('Recorded payment' if 'matched' in issues else 'Needs review'))))
        all_payments.append(item)
    payments=[item for item in all_payments if issue_filter=='all' or issue_filter in item['issues']]
    summary={
        'records':len(records),
        'matched':sum(1 for x in all_payments if 'matched' in x['issues']),
        'variance':sum(1 for x in all_payments if 'variance' in x['issues']),
        'duplicate':sum(1 for x in all_payments if 'duplicate' in x['issues']),
        'incomplete':sum(1 for x in all_payments if 'incomplete' in x['issues']),
        'processed_total':sum(float(x['processed_amount'] or 0) for x in records if x['status'] in ('Payment Processed','Closed')),
    }
    return render_template('reconciliation.html',payments=payments,q=q,issue_filter=issue_filter,summary=summary)


@app.route('/reconciliation/bank-statement', methods=['GET','POST'])
@roles_required('cashier','accounts','admin')
def bank_statement_reconciliation():
    """Match an uploaded bank CSV in memory; never save statement rows or alter refund records."""
    results=[]
    summary={'rows':0,'exact':0,'candidate':0,'unmatched':0,'duplicates':0,'variance':0}
    errors=[]
    if request.method == 'POST':
        upload=request.files.get('statement')
        if not upload or not upload.filename:
            errors.append('Choose a CSV bank statement to upload.')
        elif not upload.filename.lower().endswith('.csv'):
            errors.append('Only CSV files are supported. Export the statement from your bank as CSV.')
        else:
            try:
                raw=upload.stream.read(MAX_BYTES + 1)
                if len(raw)>MAX_BYTES:
                    raise ValueError('The CSV exceeds the 10 MB upload limit.')
                try:
                    decoded=raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    decoded=raw.decode('cp1252')
                reader=csv.DictReader(StringIO(decoded))
                if not reader.fieldnames:
                    raise ValueError('The CSV has no header row.')
                normalized={str(name or '').strip().casefold().replace('_',' ').replace('-',' '):name for name in reader.fieldnames}
                def column(*names):
                    for name in names:
                        if name in normalized: return normalized[name]
                    return None
                ref_col=column('utr','utr number','reference','reference number','transaction reference','transaction id','bank reference','cheque/reference no','txn ref')
                amount_col=column('amount','transaction amount','credit amount','credit','deposit amount','paid amount')
                date_col=column('date','transaction date','value date','payment date','txn date')
                desc_col=column('description','narration','particulars','remarks','details')
                if not ref_col or not amount_col:
                    raise ValueError('Required columns not found. Include a UTR/reference column and an Amount column. Date and Description are optional.')
                bank_rows=[]
                for line_no,row in enumerate(reader,start=2):
                    if not any(str(v or '').strip() for v in row.values()): continue
                    if len(bank_rows)>=20000: raise ValueError('The CSV contains more than 20,000 transactions. Split it into smaller files.')
                    reference=str(row.get(ref_col) or '').strip()
                    raw_amount=str(row.get(amount_col) or '').strip().replace('₹','').replace(',','').replace('INR','').strip()
                    try: amount=round(float(raw_amount),2)
                    except (TypeError,ValueError): raise ValueError(f'Invalid amount on CSV row {line_no}.')
                    date_value=str(row.get(date_col) or '').strip() if date_col else ''
                    description=str(row.get(desc_col) or '').strip() if desc_col else ''
                    bank_rows.append({'line_no':line_no,'reference':reference,'reference_key':reference.casefold(),'amount':amount,'date':date_value,'description':description})
                if not bank_rows: raise ValueError('No transaction rows were found in the CSV.')
                with db() as con:
                    app_rows=con.execute("""SELECT r.id,r.request_no,r.customer_name,r.request_location,r.status,r.payment_date,r.utr,r.processed_amount,r.requested_amount
                        FROM requests r WHERE r.status IN ('Payment Processed','Closed') OR r.payment_date IS NOT NULL OR r.utr IS NOT NULL OR r.processed_amount IS NOT NULL""").fetchall()
                app_by_ref={}
                for item in app_rows:
                    key=str(item['utr'] or '').strip().casefold()
                    if key: app_by_ref.setdefault(key,[]).append(item)
                bank_ref_counts={}
                for item in bank_rows:
                    if item['reference_key']: bank_ref_counts[item['reference_key']]=bank_ref_counts.get(item['reference_key'],0)+1
                used_app_ids=set()
                for bank in bank_rows:
                    matched=app_by_ref.get(bank['reference_key'],[]) if bank['reference_key'] else []
                    if len(matched)>1 or (bank['reference_key'] and bank_ref_counts.get(bank['reference_key'],0)>1):
                        state='duplicate'; summary['duplicates']+=1
                        candidate=matched[0] if matched else None
                    elif len(matched)==1:
                        candidate=matched[0]
                        if candidate['id'] in used_app_ids:
                            state='duplicate'; summary['duplicates']+=1
                        elif candidate['processed_amount'] is not None and abs(float(candidate['processed_amount'])-bank['amount'])>0.009:
                            state='variance'; summary['variance']+=1
                        else:
                            state='exact'; summary['exact']+=1; used_app_ids.add(candidate['id'])
                    else:
                        candidate=None
                        # Amount/date is a review-only hint, never an automatic match.
                        possible=[]
                        for item in app_rows:
                            if item['id'] in used_app_ids or item['processed_amount'] is None: continue
                            if abs(float(item['processed_amount'])-bank['amount'])>0.009: continue
                            if bank['date'] and item['payment_date'] and bank['date'][:10]!=str(item['payment_date'])[:10]: continue
                            possible.append(item)
                        if len(possible)==1:
                            candidate=possible[0]; state='candidate'; summary['candidate']+=1
                        else:
                            state='unmatched'; summary['unmatched']+=1
                    if state=='exact' and candidate: used_app_ids.add(candidate['id'])
                    results.append({'line_no':bank['line_no'],'reference':bank['reference'],'amount':bank['amount'],'date':bank['date'],'description':bank['description'],'state':state,'request':candidate})
                summary['rows']=len(bank_rows)
            except (ValueError, UnicodeDecodeError) as exc:
                errors.append(str(exc))
            except Exception:
                logging.exception('Bank statement reconciliation failed')
                errors.append('The statement could not be processed. Check the CSV format and try again.')
    return render_template('bank_reconciliation.html',results=results,summary=summary,errors=errors)


@app.route('/admin/users', methods=['GET','POST'])
@roles_required('admin')
def admin_users():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        role = request.form.get('role', '').strip()
        password = request.form.get('password', '')
        if not name or not email or role not in ('sales', 'accounts', 'admin', 'ceo'):
            flash('Enter a name, valid email, and role.', 'error')
        elif len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
        else:
            try:
                with db() as con:
                    con.execute('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',
                                (name, email, generate_password_hash(password), role))
                flash(f'{role.title()} account created for {email}.', 'success')
                return redirect(url_for('admin_users'))
            except Exception as exc:
                if isinstance(exc, sqlite3.IntegrityError) or exc.__class__.__name__ in ('UniqueViolation', 'IntegrityError'):
                    flash('An account with that email already exists.', 'error')
                else:
                    logging.exception('Admin user creation failed')
                    flash('Could not create account. Check the server logs or contact support.', 'error')
    with db() as con:
        users = con.execute('SELECT id,name,email,role FROM users ORDER BY role,name').fetchall()
    return render_template('admin_users.html', users=users)

@app.route('/admin/users/<int:user_id>/password', methods=['POST'])
@roles_required('admin')
def admin_change_password(user_id):
    password=request.form.get('password','')
    if len(password)<8:
        flash('New password must be at least 8 characters.','error')
    elif user_id == session.get('user_id'):
        flash('For security, use a separate password-change flow for your own administrator account.','error')
    else:
        try:
            with db() as con:
                cur=con.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(password),user_id))
                if getattr(cur,'rowcount',0): flash('Password updated. The employee must use the new password next time.','success')
                else: flash('Employee account not found.','error')
        except Exception:
            logging.exception('Admin password reset failed')
            flash('Password could not be updated. Check server logs.','error')
    return redirect(url_for('admin_users'))

@app.route('/requests/new',methods=['GET','POST'])
@roles_required('sales','admin')
def new_request():
    if request.method=='POST':
        try:
            amount=float(request.form.get('requested_amount','0'))
            if amount<=0: raise ValueError('Refund amount must be greater than zero.')
            if any(not request.form.get(k,'').strip() for k in ['customer_name','request_location','refund_type','reason']): raise ValueError('Please complete customer name, sales location, refund type and reason.')
            matches=duplicate_candidates(request.form)
            if matches and request.form.get('confirm_possible_duplicate') != 'yes':
                flash('Possible duplicate refund found. Review the matching requests below before submitting.','error')
                return render_template('new_request.html',duplicate_matches=matches,form_data=request.form)
            created=now()
            with db() as con:
                cur=con.execute('''INSERT INTO requests(request_no,created_at,updated_at,created_by,customer_name,customer_code,request_location,refund_type,requested_amount,reason,bank_name,account_no,ifsc_code,status,priority,ceo_approved) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id''',('RF-'+datetime.now().strftime('%y%m')+'-'+uuid.uuid4().hex[:6].upper(),created,created,session['user_id'],request.form['customer_name'].strip(),request.form.get('customer_code','').strip(),request.form.get('request_location','').strip(),request.form['refund_type'],amount,request.form['reason'].strip(),request.form.get('bank_name','').strip(),request.form.get('account_no','').strip(),request.form.get('ifsc_code','').strip().upper() or None,'Pending CEO Approval',request.form.get('priority','Normal'),0))
                rid=cur.fetchone()['id']; add_attachments(con,rid,save_uploads(request.files.getlist('attachments'),rid,'Initial supporting document')); log_action(con,rid,'Request submitted','Refund request created by sales.')
            with db() as con: row=con.execute('SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
            send_notification(ceo_recipients(),f"CEO approval required: {row['request_no']}",f"A refund request is waiting for CEO approval.\n\n{request_summary(row)}\nOpen the Refund Desk to review and decide.",rid)
            if matches:
                flash('Request submitted after duplicate review. Please verify the possible match in the request queue.','error')
            else:
                flash('Refund request submitted successfully.','success')
            return redirect(url_for('request_detail',rid=rid))
        except ValueError as e:
            flash(str(e),'error')
        except Exception:
            # PostgreSQL (psycopg), Supabase Storage and other provider errors
            # must not produce a raw traceback or leak connection details to users.
            logging.exception('Refund request submission failed')
            flash('The refund request could not be saved. Please retry; if the problem continues, contact the administrator.','error')
    return render_template('new_request.html',duplicate_matches=[],form_data={})


@app.route('/requests/<int:rid>/delete', methods=['POST'])
@roles_required('sales')
def delete_own_request(rid):
    with db() as con:
        row=con.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not row: abort(404)
        if row['created_by']!=session['user_id']: abort(403)
        if row['ceo_approved'] or row['status'] in ('CEO Approved','Under Review','Documents Required','Approved for Payment','Payment Processed','Closed'):
            flash('This request can no longer be deleted because it has entered or passed the approval/processing workflow. Ask an administrator to correct it.','error')
            return redirect(url_for('request_detail',rid=rid))
        files=con.execute('SELECT stored_name FROM attachments WHERE request_id=?',(rid,)).fetchall()
        # Remove database-linked records in one transaction; external files are cleaned up below.
        con.execute('DELETE FROM requests WHERE id=?',(rid,))
    client=storage_client()
    for file_row in files:
        stored=file_row['stored_name']
        try:
            if client:
                client.storage.from_(SUPABASE_STORAGE_BUCKET).remove([stored])
            else:
                path=UPLOAD_DIR / stored
                if path.is_file(): path.unlink()
        except Exception:
            logging.exception('Attachment cleanup failed after refund request deletion')
    flash('Your refund request was deleted because it had not been CEO-approved.','success')
    return redirect(url_for('dashboard'))

@app.route('/requests/<int:rid>/ceo-decision', methods=['POST'])
@roles_required('ceo')
def ceo_decision(rid):
    decision=request.form.get('decision','').strip()
    comments=request.form.get('comments','').strip()
    if decision not in ('Approve','Reject','Return for correction'):
        flash('Choose a valid CEO decision.','error')
        return redirect(url_for('request_detail',rid=rid))
    if decision in ('Reject','Return for correction') and not comments:
        flash('Add a comment explaining the rejection or required correction.','error')
        return redirect(url_for('request_detail',rid=rid))
    with db() as con:
        r=con.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not r: abort(404)
        if r['status'] in ('Closed','Payment Processed'):
            flash('CEO decisions cannot be changed after payment processing or closure.','error')
            return redirect(url_for('request_detail',rid=rid))
        previous_decision=r['ceo_decision'] or 'Pending'
        approved=1 if decision=='Approve' else 0
        if decision=='Reject':
            status='Rejected'
        elif decision=='Return for correction':
            status='Submitted'
        elif r['status'] in ('Pending CEO Approval','CEO Approved','Rejected','Submitted'):
            status='CEO Approved'
        else:
            # Preserve the Accounts workflow stage when the CEO re-confirms approval.
            status=r['status']
        stamp=now()
        con.execute('UPDATE requests SET status=?,ceo_approved=?,ceo_decision=?,ceo_approved_by=?,ceo_approved_at=?,ceo_comments=?,updated_at=? WHERE id=?',(status,approved,decision,session['user_id'],stamp,comments or None,stamp,rid))
        log_action(con,rid,'CEO decision changed' if previous_decision!='Pending' else 'CEO decision',f'{previous_decision} → {decision} by {session.get("name","CEO")}. {comments}'.strip())
        if comments:
            con.execute('INSERT INTO comments(request_id,user_id,created_at,visibility,body) VALUES(?,?,?,?,?)',(rid,session['user_id'],stamp,'Sales visible',f'CEO decision: {decision}. {comments}'))
        updated=con.execute('SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
    if approved:
        send_notification(accounts_recipients(),f"CEO approved refund {updated['request_no']}",f"The CEO approved this refund.\n\n{request_summary(updated)}\nAccounts may now review and process it.",rid)
    else:
        send_notification(sales_recipients(rid),f"CEO decision for refund {updated['request_no']}",f"CEO decision: {decision}.\nComments: {comments}\n\n{request_summary(updated)}",rid)
    flash('CEO decision saved and recorded in the audit history.','success')
    return redirect(url_for('request_detail',rid=rid))

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
    age_days=request_age_days(r)
    sla_days=max(1,int(os.getenv('REFUND_SLA_DAYS','3')))
    checklist=[
        {'label':'Customer name','complete':bool(r['customer_name'])},
        {'label':'Sales location','complete':bool(r['request_location'])},
        {'label':'Customer code','complete':bool(r['customer_code'])},
        {'label':'Refund reason','complete':bool(r['reason'])},
        {'label':'Supporting document','complete':bool(attachments)},
    ]
    return render_template('detail.html',r=r,comments=comments,attachments=attachments,audit=audit,age_days=age_days,sla_days=sla_days,is_overdue=is_open_status(r['status']) and age_days>=sla_days,checklist=checklist)

@app.route('/requests/<int:rid>/comment',methods=['POST'])
@login_required
def add_comment(rid):
    body=request.form.get('body','').strip(); visibility='Sales visible'; notification=None
    with db() as con:
        r=con.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not r: abort(404)
        if session['role']=='sales' and r['created_by']!=session['user_id']: abort(403)
        if not body: flash('Comment cannot be empty.','error')
        else:
            # Comments are shared with Sales, Accounts, and CEO so replies stay in one visible thread.
            con.execute('INSERT INTO comments(request_id,user_id,created_at,visibility,body) VALUES(?,?,?,?,?)',(rid,session['user_id'],now(),visibility,body))
            con.execute('UPDATE requests SET updated_at=? WHERE id=?',(now(),rid)); log_action(con,rid,'Comment added',f'{visibility} comment added.')
            if session['role'] in ('cashier','accounts','admin','ceo'):
                notification=(f"Refund update: {r['request_no']}",f"{session.get('name','A team member')} added a shared comment to {r['request_no']}.\n\n{body}\n\n{request_summary(r)}")
    if notification: send_notification(sales_recipients(rid),notification[0],notification[1],rid)
    return redirect(url_for('request_detail',rid=rid))

@app.route('/requests/<int:rid>/update',methods=['POST'])
@roles_required('cashier','accounts','admin')
def update_request(rid):
    status=request.form.get('quick_status') or request.form.get('status',''); assigned=request.form.get('assigned_to','').strip(); priority=request.form.get('priority','Normal')
    if priority not in ('Normal','High','Urgent','Super Duper Important'): priority='Normal'
    with db() as con:
        gate=con.execute('SELECT ceo_approved,status FROM requests WHERE id=?',(rid,)).fetchone()
    if not gate: abort(404)
    if not gate['ceo_approved'] or gate['status'] in ('Rejected','Closed'):
        flash('This refund is not eligible for Accounts processing until the CEO approves it.','error')
        return redirect(url_for('request_detail',rid=rid))
    if status not in STATUSES or status in ('Pending CEO Approval','CEO Approved'):
        flash('Invalid status. CEO approval statuses can only be set through the CEO decision workflow.','error')
        return redirect(url_for('request_detail',rid=rid))
    utr=request.form.get('utr','').strip(); paydate=request.form.get('payment_date','').strip(); closure=request.form.get('closure_note','').strip()
    try: processed=float(request.form['processed_amount']) if request.form.get('processed_amount','').strip() else None
    except ValueError: flash('Processed amount must be numeric.','error'); return redirect(url_for('request_detail',rid=rid))
    if status=='Payment Processed' and (not utr or not paydate or not processed or processed<=0):
        flash('To mark Payment Processed, enter payment date, UTR/reference and processed amount.','error'); return redirect(url_for('request_detail',rid=rid))
    with db() as con:
        r=con.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not r: abort(404)
        con.execute('UPDATE requests SET status=?,assigned_to=?,priority=?,utr=?,payment_date=?,processed_amount=?,closure_note=?,updated_at=? WHERE id=?',(status,assigned,priority,utr or None,paydate or None,processed,closure or None,now(),rid))
        changes=[f'Status: {r["status"]} → {status}',f'Priority: {r["priority"]} → {priority}']
        if (r['assigned_to'] or '') != assigned: changes.append(f'Assigned to: {r["assigned_to"] or "Unassigned"} → {assigned or "Unassigned"}')
        old_processed=r['processed_amount']
        if old_processed != processed: changes.append(f'Processed amount: ₹{format_inr(old_processed, 2)} → ₹{format_inr(processed, 2)}')
        if (r['utr'] or '') != (utr or ''): changes.append(f'UTR/reference changed: {"Yes" if r["utr"] else "No"} → {"Yes" if utr else "No"}')
        log_action(con,rid,'Request updated','; '.join(changes)+'.')
        if assigned and assigned != (r['assigned_to'] or ''):
            log_action(con,rid,'Request assigned',f'Assigned to {assigned}.')
        updated=con.execute('SELECT r.*,u.name AS requester FROM requests r JOIN users u ON u.id=r.created_by WHERE r.id=?',(rid,)).fetchone()
    if assigned and assigned != (r['assigned_to'] or ''):
        send_notification(sales_recipients(rid),f"Refund request assigned: {updated['request_no']}",f"The refund request is now assigned to {assigned}.\n\n{request_summary(updated)}",rid)
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
        if session['role']=='ceo': abort(403)
        try:
            saved=save_uploads(request.files.getlist('attachments'),rid,request.form.get('category','Additional document')); add_attachments(con,rid,saved)
            if saved: log_action(con,rid,'Attachment uploaded',f'{len(saved)} file(s) added.')
            flash(f'{len(saved)} file(s) uploaded.','success')
        except ValueError as e:
            flash(str(e),'error')
        except Exception:
            logging.exception('Refund attachment upload failed')
            flash('The attachment could not be uploaded. Please retry or contact the administrator.','error')
    return redirect(url_for('request_detail',rid=rid))

@app.route('/files/<int:aid>')
@login_required
def download_attachment(aid):
    with db() as con:
        a=con.execute('SELECT a.*,r.created_by FROM attachments a JOIN requests r ON r.id=a.request_id WHERE a.id=?',(aid,)).fetchone()
        if not a: abort(404)
        if session['role']=='sales' and a['created_by']!=session['user_id']: abort(403)
    client = storage_client()
    if client:
        try:
            content = client.storage.from_(SUPABASE_STORAGE_BUCKET).download(a['stored_name'])
        except Exception:
            logging.exception('Private attachment download failed')
            abort(404)
        return send_file(BytesIO(content), as_attachment=True, download_name=a['original_name'])
    return send_from_directory(UPLOAD_DIR,a['stored_name'],as_attachment=True,download_name=a['original_name'])

@app.errorhandler(413)
def too_large(_): return 'File too large. Maximum upload size is 10 MB per request.',413

init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=os.getenv('FLASK_DEBUG')=='1')
