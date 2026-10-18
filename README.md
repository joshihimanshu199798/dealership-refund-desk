# Dealership Refund Desk

Browser-based workflow for booking-amount and excess-ledger refunds: request intake, document attachments, accounts comments, status updates, UTR capture and optional SMTP notifications.

Repository: https://github.com/joshihimanshu199798/dealership-refund-desk

## Storage architecture

The app now supports:
- SQLite for local development and automated tests.
- PostgreSQL when `DATABASE_URL` is configured (psycopg 3); production mode refuses to start without it.
- Private Supabase Storage for uploaded attachments when `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are configured; production mode refuses to start without these values.
- Render free web-service blueprint with no local persistent disk. Configure the external database and storage before the first deploy.

**The integration code is committed but not yet validated against your actual Supabase project or a live deployment.** Create a PostgreSQL project and a **private** Storage bucket named `refund-attachments`. Configure the required values in Render's Environment page using `.env.example` as a guide. Never share the service-role key in chat or commit it to GitHub.

## Deployment

The Render blueprint uses the free web-service plan. Free hosting has limits and is not a production availability guarantee. External database/storage free tiers have their own quotas, inactivity policies, and recovery limitations. Use fictional data for testing until all tests pass and the dealership approves the security posture.

1. Create a Supabase project and copy its PostgreSQL connection string with TLS enabled.
2. In Supabase Storage, create a **private** bucket named `refund-attachments`.
3. Deploy this repository on Render using `render.yaml`.
4. In Render Environment, set `DATABASE_URL`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY`. Set SMTP values only if email notifications are needed.
5. Create real app users through the trusted environment using `flask --app app create-user`; valid roles are `sales`, `cashier`, `accounts`, and `admin`. The login screen has separate Sales Executive, Cashier, and Accounts/Administrator choices; the chosen login type must match the account role. Do not enable demo users in production.
6. Verify login, role boundaries, request creation, attachment upload/download, database persistence, and email delivery before using real data.

## Local checks

Install dependencies with `pip install -r requirements.txt`, then run:

```sh
python -m py_compile app.py test_smoke.py
python -m unittest -v test_smoke.py
```

A passing local test run and a successful live integration test are still required. The external PostgreSQL/Supabase integration has not yet been exercised against configured cloud services.

## Security notes

- Keep the repository private and secrets in provider environment settings only.
- Keep the Storage bucket private; files are retrieved through authenticated app routes.
- Configure a strong random `SECRET_KEY`.
- SMTP notifications are not operational until provider settings and end-to-end delivery are tested.
- This remains a pilot MVP, not a security-approved production system for customer or banking data.


## Refund workflow enhancements

- **Duplicate review:** new submissions are checked against active requests using customer code, or customer name + refund type + amount (within ₹1). Matches are warnings, not automatic rejections; staff must explicitly confirm a legitimate separate request.
- **Submission checklist:** request details show missing customer code or supporting-document reminders alongside core field completeness. Customer code and documents remain review prompts rather than hard requirements.
- **Ageing / overdue queue:** dashboard shows request age and an overdue shortcut. The default SLA threshold is 3 days; configure `REFUND_SLA_DAYS` in the hosting environment to change it. Only open requests are counted as overdue; processed, closed and rejected requests are excluded.
- **Accounts quick actions:** Accounts/Cashier/Admin can move requests to Under Review, Documents Required, or Approved for Payment without changing pages. Payment Processed still requires a payment date, UTR and processed amount.
- **Email notifications:** existing SMTP notifications cover new requests and status changes; assignment changes also notify the request creator. Delivery requires valid SMTP environment settings and is logged. Configure SMTP credentials only in Render environment settings, never in the repository.

The new checks are intended to flag possible problems for human review, not independently approve, reject or pay refunds. Validate role permissions and email delivery in a test deployment before using real customer or banking information.
