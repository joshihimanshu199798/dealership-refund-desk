"""Basic smoke checks. Run with: python -m unittest test_smoke.py"""
import os, tempfile, unittest
from pathlib import Path

class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        os.environ.pop('DATABASE_URL', None)
        os.environ.pop('SUPABASE_URL', None)
        os.environ.pop('SUPABASE_SERVICE_ROLE_KEY', None)
        os.environ['DATABASE_PATH']=str(Path(self.tmp.name)/'test.db')
        os.environ['UPLOAD_DIR']=str(Path(self.tmp.name)/'uploads')
        os.environ['DEMO_MODE']='true'
        os.environ['SECRET_KEY']='test-secret'
        import app as app_module
        app_module.DB_PATH=Path(os.environ['DATABASE_PATH'])
        app_module.UPLOAD_DIR=Path(os.environ['UPLOAD_DIR'])
        app_module.UPLOAD_DIR.mkdir(exist_ok=True)
        app_module.init_db()
        app_module.app.config.update(TESTING=True,SECRET_KEY='test-secret')
        self.app_module=app_module
        self.client=app_module.app.test_client()
    def tearDown(self): self.tmp.cleanup()
    def login(self,email,password): return self.client.post('/login',data={'email':email,'password':password},follow_redirects=True)
    def test_health_check_confirms_database_is_reachable(self):
        r=self.client.get('/healthz')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.get_json(),{'status':'ok','database':'ok'})

    def test_sales_login_and_dashboard(self):
        r=self.login('sales@example.com','SalesDemo123!')
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Refund requests',r.data)
    def test_sales_can_create_request(self):
        self.login('sales@example.com','SalesDemo123!')
        r=self.client.post('/requests/new',data={'customer_name':'Test Customer','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'25000','reason':'Test refund'},follow_redirects=True)
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Test Customer',r.data)
        with self.app_module.db() as con:
            row=con.execute('SELECT * FROM requests').fetchone()
            self.assertEqual(row['status'],'Submitted')
    def test_possible_duplicate_requires_explicit_confirmation(self):
        self.login('sales@example.com','SalesDemo123!')
        payload={'customer_name':'Repeat Customer','customer_code':'C-102','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'12500','reason':'Test refund'}
        first=self.client.post('/requests/new',data=payload,follow_redirects=True)
        self.assertEqual(first.status_code,200)
        warning=self.client.post('/requests/new',data=payload,follow_redirects=True)
        self.assertIn(b'Possible duplicate refund detected',warning.data)
        with self.app_module.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) AS count FROM requests').fetchone()['count'],1)
        confirmed=dict(payload); confirmed['confirm_possible_duplicate']='yes'
        result=self.client.post('/requests/new',data=confirmed,follow_redirects=True)
        self.assertEqual(result.status_code,200)
        with self.app_module.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) AS count FROM requests').fetchone()['count'],2)

    def test_accounts_quick_action_updates_status(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Quick Action Customer','request_location':'Delhi Test Branch','refund_type':'Excess ledger balance','requested_amount':'500','reason':'Test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        result=self.client.post(f'/requests/{rid}/update',data={'status':'Submitted','quick_status':'Under Review','assigned_to':'Accounts Team','priority':'Normal'},follow_redirects=True)
        self.assertEqual(result.status_code,200)
        with self.app_module.db() as con:
            self.assertEqual(con.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()['status'],'Under Review')

    def test_sales_cannot_open_accounts_update(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Test Customer','request_location':'Delhi Test Branch','refund_type':'Excess ledger balance','requested_amount':'500','reason':'Test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        r=self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed'},follow_redirects=True)
        self.assertEqual(r.status_code,403)
    def test_processed_requires_utr_date_and_amount(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Test Customer','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'500','reason':'Test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        r=self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed'},follow_redirects=True)
        self.assertIn(b'enter payment date, UTR/reference and processed amount',r.data)
        with self.app_module.db() as con: self.assertEqual(con.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()['status'],'Submitted')

if __name__=='__main__': unittest.main()
