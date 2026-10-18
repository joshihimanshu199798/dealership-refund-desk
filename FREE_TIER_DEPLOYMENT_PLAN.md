# ₹0 initial test deployment plan

## Do not deploy the current app for real data yet
The app now has a PostgreSQL backend selected by DATABASE_URL and a private Supabase Storage backend selected by SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY. Without those settings it uses SQLite/local files for development. Production mode now refuses to start without PostgreSQL and Supabase Storage configuration. The code integration is committed, but it has not yet been validated against a real Supabase project or a live deployment.

## Target architecture for a fictional-data pilot
- Web service: Render free web service. It may sleep after inactivity, has limited resources, and is not intended for production reliability.
- Database: Supabase Free PostgreSQL. Confirm current quotas, pause/inactivity policy, backup/export options and project limits in the provider dashboard. Free tier is not a guarantee of indefinite availability or backups.
- Attachments: Private Supabase Storage bucket with server-side access checks. Never make customer documents public. Check storage and bandwidth quotas before use.
- Email: Optional SMTP integration; notifications are not operational until configured and tested with controlled mailboxes.

## Required implementation gates before deployment
1. Replace SQLite access with a PostgreSQL-compatible database layer and use DATABASE_URL with TLS.
2. Store attachments in a private object-storage bucket; keep only object keys and metadata in the database. Enforce role/ownership checks for upload/download and limit file size/type.
3. Remove seeded public demo accounts from any production deployment. Add administrator-controlled user provisioning, password reset, CSRF protection, secure cookie settings, rate limiting, and audit events.
4. Add database migrations, backups/export instructions, error handling, and tests for role separation, uploads, persistence after redeploy, and email delivery.
5. Configure secrets only in Render environment settings; never commit them to GitHub or paste them into chat.
6. Test using fictional data. Only move to real customer data after dealership IT/security approval.

## Free-tier trade-offs
Free services can sleep, have quotas and may change policies. Free database projects can have inactivity pauses and limited backup/recovery guarantees. A free-tier setup can be suitable for a limited demonstration, but should not be described as production-grade durable storage. Keep independent encrypted exports and verify restore procedures.

## Current verified state
The GitHub repository now contains PostgreSQL and Supabase Storage integration code, a Render free-tier blueprint and smoke tests. Cloud credentials and a private bucket still need to be configured; no live app deployment or cloud integration test has been verified. Functional tests and email delivery are not yet confirmed.
