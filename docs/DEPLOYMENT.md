# Development and deployment

## 1. GitHub Codespaces

The repository includes `.devcontainer/devcontainer.json`. In GitHub, open the repository,
select **Code → Codespaces → Create codespace**, then run:

```bash
cd backend
uvicorn investment_town.main:app --host 0.0.0.0 --port 8000 --reload
```

Open the forwarded port from the Codespaces **Ports** panel. Keep its visibility private.
MVP 1 does not need brokerage credentials. Do not add Toss Securities credentials to a
Codespace.

## 2. Railway MVP 1

Create a Railway project from the GitHub repository and deploy the repository root. Railway
will use the root `Dockerfile` and `railway.json`.

Configure these service variables:

```text
APP_ENV=production
CONTROL_API_TOKEN=<a long random value>
DATABASE_PATH=/data/investment-town.db
PAPER_TRADING_ONLY=true
TOSSINVEST_LIVE_ENABLED=false
```

Attach one Railway volume to the service at `/data`, then generate an HTTPS domain. Use only
one service replica: MVP 1 uses SQLite and an in-process WebSocket event hub. PostgreSQL and
Redis are required before horizontal scaling.

Do not configure any of these values during MVP 1:

```text
TOSSINVEST_CLIENT_ID
TOSSINVEST_CLIENT_SECRET
TOSSINVEST_ACCOUNT_SEQ
```

After deployment, verify:

```bash
curl https://<railway-domain>/api/v1/health
curl -H "Authorization: Bearer <CONTROL_API_TOKEN>" \
  https://<railway-domain>/api/v1/projects
```

The first response must report `paper_trading_only: true` and
`live_trading_implemented: false`.

## 3. Before live trading

Move the runtime to a personal AWS account or a dedicated personal machine. Replace SQLite
and the in-process event hub with PostgreSQL and Redis, store credentials in a secret manager,
and restrict administration through a private network. Follow
[`TOSS_LIVE_TRADING_PLAN.md`](TOSS_LIVE_TRADING_PLAN.md); never reuse the Railway MVP service
for live brokerage execution.

## Official references

- [GitHub Codespaces secrets](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-your-account-specific-secrets-for-github-codespaces)
- [Railway Dockerfiles](https://docs.railway.com/guides/dockerfiles)
- [Railway variables](https://docs.railway.com/guides/variables)
- [Railway volumes](https://docs.railway.com/guides/volumes)
- [Railway config as code](https://docs.railway.com/reference/config-as-code)
