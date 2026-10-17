# Deploy Dealership Refund Desk to Render

This repository uses a Docker-based Render web service and persistent disk mounted at `/data`. A persistent disk generally requires a paid Render service.

## Deploy
1. Sign in to https://dashboard.render.com/ and choose **New + → Blueprint**.
2. Connect GitHub and select `dealership-refund-desk`.
3. Review `render.yaml`, service plan and persistent disk before applying.
4. Wait for build/deploy; open the generated HTTPS URL and check `/healthz` returns `{"status":"ok"}`.

## Demo-only login warning
The current MVP seeds demo users on first database creation:
- `sales@example.com` / `SalesDemo123!`
- `accounts@example.com` / `AccountsDemo123!`
- `admin@example.com` / `AdminDemo123!`

Use fictional data only. Do not use a publicly reachable deployment with these demo credentials for real customer/bank documents. Replace demo users, review authentication/access controls, CSRF protections, secure uploads, backups and logging before any real-data pilot.

## Email configuration
Email is sent only when SMTP is configured. In Render → service → Environment, configure approved values: `SMTP_HOST`, `SMTP_PORT` (usually `587`), `SMTP_FROM`, `SMTP_USERNAME`, `SMTP_PASSWORD` (secret), `SMTP_STARTTLS` (`true`), and optionally `ACCOUNTS_NOTIFICATION_EMAILS` (comma-separated). Save and redeploy, then test delivery using controlled mailboxes. Never commit credentials to GitHub.

## Limitations
- Browser-based responsive application; no desktop or phone app installation.
- Sales can submit requests and documents; accounts can comment, request documents, update status, and record payment date/amount/UTR.
- Email notifications are conditional on SMTP configuration; delivery is not live until configured and tested.
- No incoming email auto-import, DMS integration, bank/payment execution, malware scanning, or full production hardening.
- SQLite with persistent disk is suitable only for a small controlled pilot; evaluate managed PostgreSQL and private object storage before broad use.

## Checks
Run `python -m py_compile app.py test_smoke.py`, install dependencies, then run `python -m unittest test_smoke.py`. Syntax checks alone are not functional tests.
