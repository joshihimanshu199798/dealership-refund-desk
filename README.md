# Dealership Refund Desk

Browser-based MVP for tracking booking-amount and excess-ledger refunds: request intake, document attachments, accounts comments, status updates, UTR capture and conditional SMTP notifications.

Repository: https://github.com/joshihimanshu199798/dealership-refund-desk

## Status
This is an MVP and is NOT ready for real customer or banking data. It currently uses SQLite and local file uploads. Render free web services do not provide persistent disks, so local records/uploads may be lost after restart/redeploy. PostgreSQL and private object storage must be implemented and verified before deploying for durable use.

## Free-tier deployment planning
Read FREE_TIER_DEPLOYMENT_PLAN.md before deploying. Do not assume that adding database/storage environment variables alone changes application storage.

## Render
Deployment configuration is in render.yaml. Before using it, review the current plan and storage requirements; a paid Render service is required for Render persistent disks. For an initial ₹0 demo, implement and test external PostgreSQL and private object storage first.

## Local checks
Install dependencies with pip install -r requirements.txt, then run python -m py_compile app.py test_smoke.py and python -m unittest test_smoke.py. These commands have not been verified in the current deployment environment. Do not report a test as passed until it has actually run.

## Security
Demo credentials are present in the MVP and must not be used on a public deployment for real data. Configure a strong random SECRET_KEY through the hosting provider. SMTP notifications require provider configuration and successful delivery testing. Never commit credentials.
