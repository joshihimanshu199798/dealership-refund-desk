# Dealership Refund Desk

Browser-based dealership booking and excess-ledger refund workflow.

## Deploy on Render
1. Open https://dashboard.render.com/ and choose **New + → Blueprint**.
2. Select this repository and approve the `render.yaml` configuration.
3. Configure the required environment variables and persistent disk as shown in the deployment guide.
4. Deploy and verify `/health` before using the application.

## Important
This application is an MVP. Do not enter real customer/bank data until authentication, access controls, persistent database storage, secure document storage, backups, and email delivery are reviewed and tested. Email is disabled until an SMTP provider is configured.

See `DEPLOY_TO_RENDER.md` for details.
