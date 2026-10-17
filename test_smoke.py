"""Basic smoke checks. Run with: python -m unittest test_smoke.py"""
import os, tempfile, unittest
from pathlib import Path

class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        os.environ['DATABASE_PATH']=str(Path(self.tmp.name)/'test.db')
        os.environ['UPLOAD_DIR']=str(Path(self.tmp.name)/'uploads')
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
    def test_sales_login_and_dashboard(self):
        r=self.login('sales@example.com','SalesDemo123!')
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Refund requests',r.data)
    def test_sales_can_create_request(self):
        self.login('sales@example.com','SalesDemo123!')
        r=self.client.post('/requests/new',data={'customer_name':'Test Customer','refund_type':'Booking amount refund','requested_amount':'25000','reason':'Test refund'},follow_redirects=True)
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Test Customer',r.data)
        with self.app_module.db() as con:
            row=con.execute('SELECT * FROM requests').fetchone()
            self.assertEqual(row['status'],'Submitted')
    def test_sales_cannot_open_accounts_update(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Test Customer','refund_type':'Excess ledger balance','requested_amount':'500','reason':'Test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        r=self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed'},follow_redirects=True)
        self.assertEqual(r.status_code,403)
    def test_processed_requires_utr_date_and_amount(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Test Customer','refund_type':'Booking amount refund','requested_amount':'500','reason':'Test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        r=self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed'},follow_redirects=True)
        self.assertIn(b'enter payment date, UTR/reference and processed amount',r.data)
        with self.app_module.db() as con: self.assertEqual(con.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()['status'],'Submitted')

if __name__=='__main__': unittest.main()
