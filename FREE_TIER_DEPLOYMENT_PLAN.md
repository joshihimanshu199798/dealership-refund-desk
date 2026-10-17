# ₹0 initial test deployment plan

## Do not deploy the current app for real data yet
The current code uses SQLite and writes attachments to a local folder. Render's free web service does not provide persistent disks; local filesystem data must be treated as ephemeral. Simply adding DATABASE_URL or a storage key to Render does not make the current app use those services. This repository needs a verified PostgreSQL and object-storage implementation before the free-tier deployment can safely store records and attachments.

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
The GitHub repository contains a Flask MVP, templates, a Dockerfile, Render configuration and smoke-test source. This plan file does not itself implement PostgreSQL or object storage. The live app has not been deployed, and functional tests/email delivery have not been verified.
