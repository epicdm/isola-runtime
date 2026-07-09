# Runbook — Clawith staging shared secret (PKT-04 / decision d35)

Status: code change only. No secret has been created, viewed, copied, or
applied by the agent that authored this change. Everything below is an
**operator-only** action to be performed later, by a human with access to
the real staging environment.

## Background

The BFF -> Clawith dispatch endpoint (`POST /api/internal/dispatch` on
isola-runtime, port `:8800`) is gated by a single shared secret,
`BFF_CLAWITH_SHARED_SECRET`, checked via `Authorization: Bearer <secret>`.
Only one value has ever been provisioned — the production one. The
staging BFF (`bff-v2-staging`, branch `p0-0/wa-send-gate`) presents a
different value (or none), so all of its calls to `/api/internal/dispatch`
401, which is the root cause of the 9 failing spine tests referenced in
decision d35.

This change (Option C from d35) adds support for a **second, staging-only**
secret, `BFF_CLAWITH_SHARED_SECRET_STAGING`, checked alongside the existing
prod secret. It does not change the endpoint, the header scheme, or any
other caller's behavior. See `backend/app/core/bff_clawith_auth.py` for the
comparison logic and `backend/tests/test_bff_clawith_staging_auth.py` for
the tests proving prod behavior is unchanged.

## What the operator must do (staging only — do this after merge + deploy)

1. Generate a new random secret value (e.g. `openssl rand -hex 32`).
   This must be a **different value** from the production
   `BFF_CLAWITH_SHARED_SECRET` — do not reuse the prod secret here.
2. On deepseek, in the staging Clawith deployment's env (wherever
   `BFF_CLAWITH_SHARED_SECRET` is currently set for the shared `:8800`
   instance — confirm the exact env file/compose override in use at
   deploy time, since this repo's `docker-compose.override.yml` is a
   local-dev template, not the deployed staging config), set:
   ```
   BFF_CLAWITH_SHARED_SECRET_STAGING=<the new value>
   ```
3. On the `bff-v2-staging` side (`p0-0/wa-send-gate`, `/home/epicdm/bff-v2-staging`),
   set the BFF's outbound dispatch-call secret to the **same** new value
   (the env var name on the BFF side may differ — confirm against that
   repo's config before setting).
4. Recreate (not just restart) the container(s) so the new env var is
   picked up, per this project's "Docker env at CREATE" apparatus rule
   (`docker compose up -d --force-recreate`, not `docker restart`).
5. Re-run the staging spine suite
   (`scripts/spine/run-all.sh` on the staging checkout) and confirm the
   9 previously-failing Clawith-auth cases now pass (target: 118/0).

No production file, container, or credential is touched by steps 1-5 —
they are entirely scoped to the staging BFF and the staging value of this
one new env var on the shared Clawith instance.

## Rollback

- **Code**: revert this PR (or `git revert` the merge commit). The
  endpoint reverts to accepting only `BFF_CLAWITH_SHARED_SECRET`; nothing
  else changes.
- **Staging env**: unset/remove `BFF_CLAWITH_SHARED_SECRET_STAGING` from
  the staging env and recreate the container. With the env var absent,
  `any_bff_clawith_secret_configured()` / `resolve_bff_clawith_secret()`
  behave exactly as the pre-change code did (prod secret only).
- No data migration, schema change, or prod-facing behavior is involved
  in either direction.

## Explicit non-actions (by design, for this PKT)

- No production environment, container, or credential was accessed,
  created, or changed.
- No secret value was generated, viewed, copied, or transmitted by the
  authoring agent.
- No staging configuration was applied.
- No container or service was restarted or recreated.
- No Docker socket access was used.
- Nothing was merged or deployed — this PR is opened as a draft.
