# Deployment

Two services, three secrets. The blueprint creates both services; the agent needs the
portal's public URL and its own operator token.

## Render

1. Push this repository to GitHub.
2. Generate the secrets: `python -m scripts.gen_secrets`. This writes
   `deploy/.secrets.env` (gitignored) with `RTI_APPROVAL_SECRET`,
   `RTI_ADMIN_TOKEN` and `RTI_OPERATOR_TOKEN`. The values are never printed.
3. Render → **New → Blueprint**, pick the repository. Render reads
   `deploy/render.yaml` and creates both services on the Free plan.
4. On **rti-pilot-portal**, set:
   - `RTI_APPROVAL_SECRET` — from `deploy/.secrets.env`
   - `RTI_ADMIN_TOKEN` — from `deploy/.secrets.env`
5. On **rti-pilot-agent**, set the **same** `RTI_APPROVAL_SECRET`, plus
   `RTI_OPERATOR_TOKEN` (from the same file) and `RTI_PORTAL_URL` =
   `https://rti-pilot-portal.onrender.com` (the service name in the blueprint fixes this
   host). Do not enter a placeholder: `RTI_ENV=production` refuses to boot with one, and
   that refusal appears in the deploy log as a clear error.
6. The agent runs in `http` filing mode, so it does not install or launch Chromium.
7. Warm both services by requesting the portal `/healthz` and agent `/api/health`.
   After an idle period the first request can take about a minute to wake a service;
   run the evaluation promptly after both health checks respond. Set local
   `RTI_AGENT_URL` and `RTI_PORTAL_URL` to the public URLs, then run
   `python -m scripts.run_eval`.

### Free-tier limits (important)

- The 750 free instance-hours are shared across the workspace. Two services that
  remain awake all month would exceed that allowance; idle services spin down after
  15 minutes without inbound traffic and take roughly a minute to wake.
- The filesystem is ephemeral and free services cannot attach persistent disks.
  `agent.db` and `portal.db` may be reset after a restart, redeploy, or spin-down.
  Treat all deployed data and evaluation results as disposable; run the evaluation
  soon after both services wake.
- This free configuration is for demos, not durable production data or an always-on
  service. The HTTP transport preserves the mock filing/evaluation flow but does not
  demonstrate browser automation.

## Railway

1. Create two services from the same repository.
2. Set the service's Dockerfile path to `deploy/Dockerfile.portal` and
   `deploy/Dockerfile.agent` respectively.
3. Add a volume mounted at `/app/data` to each service.
4. Set the same environment variables as above. Railway injects `PORT`; both images
   honour it.
5. Set `RTI_PORTAL_URL` on the agent to the portal's public domain.

## Fly.io

```bash
fly launch --dockerfile deploy/Dockerfile.portal --name rti-pilot-portal
fly launch --dockerfile deploy/Dockerfile.agent  --name rti-pilot-agent --vm-memory 2048
fly volumes create rti_portal_data --size 1 --app rti-pilot-portal
fly volumes create rti_agent_data  --size 1 --app rti-pilot-agent
```

Mount the volumes at `/app/data`, set the same `RTI_APPROVAL_SECRET` with
`fly secrets set`, and set `RTI_PORTAL_URL` on the agent.

## Checks after deploying

- `GET /healthz` on the portal and `GET /api/health` on the agent.
- `GET /api/ready` on the agent returns 200 and names the portal it can reach. A 503
  carries the reason, so a placeholder URL or an unreachable portal is visible at once.
- `GET /api/runs` without a token returns **401**. With `X-Operator-Token` it returns runs.
- Open `/admin/clock` on the portal: it asks for a sign-in instead of rendering the
  admin token, and an unsigned `POST /admin/clock` returns 403.
- Open the portal `/apply` page: the MOCK banner must be visible.
- File TC1 from the agent UI: approve, complete the CAPTCHA from the dashboard, and
  confirm the verifier shows observed values.
- Confirm a token-less payment is rejected by the portal.
- Confirm `RTI_APPROVAL_SECRET`, `RTI_ADMIN_TOKEN` and `RTI_OPERATOR_TOKEN` are all
  different from the repository defaults and from each other (production refuses to start
  otherwise).
- Click **Operator access** in the dashboard and paste the deployed `RTI_OPERATOR_TOKEN`:
  the review queue, tracker and test tabs load only after that.
- Create a real reviewer account with `python -m scripts.operator add --name <name>`
  against the deployed database (or keep the env token and revoke it once accounts
  exist). Open a flagged request, claim it, release or uphold it, and confirm `/api/audit`
  names the account.

## What not to do

- Do not expect the Free plan to preserve database files or remain awake 24/7.
- Do not point the agent at a real government portal. The filing client is written for
  the mock form and the plan explicitly excludes real portals.
- Do not expose the demo console as a citizen-facing surface. It is operator-only and
  behind a token sign-in.
