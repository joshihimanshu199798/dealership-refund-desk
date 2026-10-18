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
    def login(self,email,password):
        role='sales' if email.startswith('sales') else ('accounts' if email.startswith('accounts') else ('ceo' if email.startswith('ceo') else 'admin'))
        return self.client.post('/login',data={'email':email,'password':password,'login_role':role},follow_redirects=True)
    def approve_as_ceo(self,rid):
        self.client.get('/logout')
        self.login('ceo@example.com','CEOdemo123!!')
        return self.client.post(f'/requests/{rid}/ceo-decision',data={'decision':'Approve','comments':'Approved for payment review'},follow_redirects=True)

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
            self.assertEqual(row['status'],'Pending CEO Approval')
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

    def test_accounts_cannot_process_before_ceo_approval(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Pending CEO Customer','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'1200','reason':'Waiting for approval'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        result=self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed','processed_amount':'1200','payment_date':'2026-10-18','utr':'TESTUTR'},follow_redirects=True)
        self.assertIn(b'not eligible for Accounts processing',result.data)
        with self.app_module.db() as con:
            row=con.execute('SELECT status,ceo_approved FROM requests WHERE id=?',(rid,)).fetchone()
            self.assertEqual(row['status'],'Pending CEO Approval')
            self.assertEqual(row['ceo_approved'],0)

    def test_ceo_approval_is_audited(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'CEO Audit Customer','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'1200','reason':'CEO audit test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        response=self.approve_as_ceo(rid)
        self.assertEqual(response.status_code,200)
        with self.app_module.db() as con:
            row=con.execute('SELECT status,ceo_approved,ceo_decision,ceo_approved_by,ceo_approved_at FROM requests WHERE id=?',(rid,)).fetchone()
            audit=con.execute("SELECT action,details FROM audit_log WHERE request_id=? AND action='CEO decision'",(rid,)).fetchone()
            self.assertEqual(row['status'],'CEO Approved')
            self.assertEqual(row['ceo_approved'],1)
            self.assertEqual(row['ceo_decision'],'Approve')
            self.assertIsNotNone(row['ceo_approved_by'])
            self.assertIsNotNone(row['ceo_approved_at'])
            self.assertIsNotNone(audit)

    def test_accounts_quick_action_updates_status(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Quick Action Customer','request_location':'Delhi Test Branch','refund_type':'Excess ledger balance','requested_amount':'500','reason':'Test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        self.approve_as_ceo(rid)
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        result=self.client.post(f'/requests/{rid}/update',data={'status':'Submitted','quick_status':'Under Review','assigned_to':'Accounts Team','priority':'Normal'},follow_redirects=True)
        self.assertEqual(result.status_code,200)
        with self.app_module.db() as con:
            self.assertEqual(con.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()['status'],'Under Review')


    def test_accounts_can_export_filtered_refund_csv(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'CSV Test Customer','customer_code':'CSV-1','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'2500','reason':'CSV export test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        self.approve_as_ceo(rid)
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        response=self.client.get('/reports/refunds.csv?status=CEO+Approved')
        self.assertEqual(response.status_code,200)
        self.assertIn('text/csv',response.content_type)
        self.assertIn(b'CSV Test Customer',response.data)
        self.assertIn(b'Request number',response.data)

    def test_sales_cannot_export_refund_csv(self):
        self.login('sales@example.com','SalesDemo123!')
        response=self.client.get('/reports/refunds.csv')
        self.assertEqual(response.status_code,403)

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
        self.approve_as_ceo(rid)
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        r=self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed'},follow_redirects=True)
        self.assertIn(b'enter payment date, UTR/reference and processed amount',r.data)
        with self.app_module.db() as con: self.assertEqual(con.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()['status'],'CEO Approved')


    def test_dashboard_control_centre_and_analytics_render(self):
        self.login('admin@example.com','AdminDemo123!')
        dashboard=self.client.get('/dashboard')
        self.assertEqual(dashboard.status_code,200)
        self.assertIn(b'Smart Refund Control Centre',dashboard.data)
        analytics=self.client.get('/dashboard?view=analytics')
        self.assertEqual(analytics.status_code,200)
        self.assertIn(b'Six-month trend',analytics.data)
        self.assertIn(b'Branch / location exposure',analytics.data)


    def test_indian_currency_formatting(self):
        self.assertEqual(self.app_module.format_inr(1234567.89), '12,34,567.89')
        self.assertEqual(self.app_module.format_inr(100000, 0), '1,00,000')
        self.assertEqual(self.app_module.format_inr(-1234.5), '-1,234.50')

    def test_audit_trail_is_available_to_ceo_and_admin_only(self):
        self.login('admin@example.com','AdminDemo123!')
        admin_response=self.client.get('/audit')
        self.assertEqual(admin_response.status_code,200)
        self.assertIn(b'Audit trail',admin_response.data)
        self.client.get('/logout')
        self.login('sales@example.com','SalesDemo123!')
        self.assertEqual(self.client.get('/audit').status_code,403)
        self.client.get('/logout')
        self.login('ceo@example.com','CEOdemo123!!')
        self.assertEqual(self.client.get('/audit').status_code,200)

    def test_dashboard_displays_indian_currency_grouping(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Indian Format Customer','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'1234567.89','reason':'Indian format test'},follow_redirects=True)
        response=self.client.get('/dashboard?view=requests')
        self.assertEqual(response.status_code,200)
        self.assertIn('₹12,34,567.89'.encode(),response.data)


    def test_payment_reconciliation_flags_variance_and_duplicate_utr(self):
        self.login('sales@example.com','SalesDemo123!')
        for name,amount in [('Variance Customer','1200'),('Duplicate Reference Customer','1200')]:
            self.client.post('/requests/new',data={'customer_name':name,'request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':amount,'reason':'Reconciliation test'},follow_redirects=True)
        with self.app_module.db() as con:
            ids=[row['id'] for row in con.execute('SELECT id FROM requests ORDER BY id').fetchall()]
        for rid in ids:
            self.approve_as_ceo(rid)
            self.client.get('/logout')
            self.login('accounts@example.com','AccountsDemo123!')
            amount='1000' if rid==ids[0] else '1200'
            self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed','processed_amount':amount,'payment_date':'2026-10-19','utr':'DUPLICATE-UTR'},follow_redirects=True)
            self.client.get('/logout')
            self.login('sales@example.com','SalesDemo123!')
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        page=self.client.get('/reconciliation')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'Payment reconciliation',page.data)
        self.assertIn(b'Amount variance',page.data)
        self.assertIn(b'Duplicate UTR',page.data)
        variance=self.client.get('/reconciliation?issue=variance')
        self.assertIn(b'Variance Customer',variance.data)
        self.assertNotIn(b'Duplicate Reference Customer',variance.data)

    def test_bank_statement_csv_matches_reference_and_flags_variance(self):
        self.login('sales@example.com','SalesDemo123!')
        self.client.post('/requests/new',data={'customer_name':'Bank Match Customer','request_location':'Delhi Test Branch','refund_type':'Booking amount refund','requested_amount':'1200','reason':'Bank CSV test'},follow_redirects=True)
        with self.app_module.db() as con: rid=con.execute('SELECT id FROM requests').fetchone()['id']
        self.approve_as_ceo(rid)
        self.client.get('/logout')
        self.login('accounts@example.com','AccountsDemo123!')
        self.client.post(f'/requests/{rid}/update',data={'status':'Payment Processed','processed_amount':'1200','payment_date':'2026-10-19','utr':'BANK-REF-123'},follow_redirects=True)
        import io
        csv_data='Date,UTR,Amount,Description\\n2026-10-19,BANK-REF-123,1200,Refund payout\\n2026-10-19,UNKNOWN-REF,500,Unknown payout\\n2026-10-19,BANK-REF-123,1200,Duplicate payout\\n'
        response=self.client.post('/reconciliation/bank-statement',data={'statement':(io.BytesIO(csv_data.encode()),'bank.csv')},content_type='multipart/form-data')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'Reference + amount matched',response.data)
        self.assertIn(b'Unmatched',response.data)
        self.assertIn(b'Duplicate reference',response.data)
        with self.app_module.db() as con:
            row=con.execute('SELECT status,utr FROM requests WHERE id=?',(rid,)).fetchone()
            self.assertEqual(row['status'],'Payment Processed')
            self.assertEqual(row['utr'],'BANK-REF-123')

    def test_bank_statement_upload_validates_format_and_access(self):
        self.login('accounts@example.com','AccountsDemo123!')
        import io
        bad=self.client.post('/reconciliation/bank-statement',data={'statement':(io.BytesIO(b'Wrong,Columns\\nabc,123\\n'),'bank.csv')},content_type='multipart/form-data')
        self.assertEqual(bad.status_code,200)
        self.assertIn(b'UTR/reference column and an Amount column',bad.data)
        self.client.get('/logout')
        self.login('sales@example.com','SalesDemo123!')
        self.assertEqual(self.client.get('/reconciliation/bank-statement').status_code,403)

    def test_sales_cannot_open_payment_reconciliation(self):
        self.login('sales@example.com','SalesDemo123!')
        self.assertEqual(self.client.get('/reconciliation').status_code,403)

if __name__=='__main__': unittest.main()
