# Deployment

Two services, one shared secret. The blueprint creates both services; set the portal URL on the agent after Render assigns the portal's public URL.

## Render

1. Push this repository to GitHub.
2. Render → **New → Blueprint**, pick the repository. Render reads
   `deploy/render.yaml` and creates both services on the Free plan. If prompted for
   `RTI_PORTAL_URL` before the portal exists, enter `https://example.invalid` as a
   temporary placeholder; replace it after the portal gets its public URL.
3. On **rti-pilot-portal**, set:
   - `RTI_APPROVAL_SECRET` — long random string
   - `RTI_ADMIN_TOKEN` — long random string
4. On **rti-pilot-agent**, set the *same* `RTI_APPROVAL_SECRET` and
   `RTI_ADMIN_TOKEN`. Once Render assigns the portal URL, set the agent's
   `RTI_PORTAL_URL` to that actual public URL and save it.
5. The agent runs in `http` filing mode, so it does not install or launch Chromium.
6. Warm both services by requesting the portal `/healthz` and agent `/api/health`.
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
- Open the portal `/apply` page: the MOCK banner must be visible.
- File TC1 from the agent UI: approve, complete the CAPTCHA from the dashboard, and
  confirm the verifier shows observed values.
- Confirm a token-less payment is rejected by the portal.
- Confirm `RTI_APPROVAL_SECRET` differs from the repository default.
- Confirm `RTI_ADMIN_TOKEN` is not the default.

## What not to do

- Do not expect the Free plan to preserve database files or remain awake 24/7.
- Do not point the agent at a real government portal. The filing client is written for
  the mock form and the plan explicitly excludes real portals.
- Do not expose the demo console as a citizen-facing surface. It is operator-only and
  guarded by the admin token.
