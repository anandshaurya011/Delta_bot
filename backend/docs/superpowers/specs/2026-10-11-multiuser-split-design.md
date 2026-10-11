# Split frontend/backend, Clerk login, per-user Delta credentials

Date: 2026-10-11

## Goal
Turn the single-user FastAPI + vanilla-HTML app into two projects: the existing FastAPI backend (API only) and a new Next.js/TypeScript frontend. Users sign in with Clerk and each saves their own Delta Exchange API key/secret in a Settings page. Every Delta call and audit-log entry is scoped to the signed-in user.

## Assumptions
- Anyone who can sign up may use the app; each user brings their own Delta keys.
- Next.js (App Router) + TypeScript chosen by the user.
- Clerk keys live only in git-ignored `.env` files (`frontend/.env.local`, `backend/.env`). They are never committed or logged.
- Existing `data.json` audit events are not migrated.

## Architecture
```
Browser -> Next.js (clerkMiddleware, React/TS) -> FastAPI (localhost) -> Delta Exchange
           /api/[...path] route handler:          verifies Clerk JWT,
           attaches Clerk session token           loads that user's keys
```
- Browser talks only to Next.js. A catch-all `/api/[...path]` route handler forwards to FastAPI with `Authorization: Bearer <Clerk session token>`.
- FastAPI binds to 127.0.0.1. The shared `X-API-Key` and the `/ui` static mount are removed.
- Layout: `delta/backend/` (API), `delta/frontend/` (Next.js app).

## Backend
- **Auth:** replace `require_api_key` with a dependency verifying the Clerk JWT against Clerk's JWKS (PyJWT[crypto] + httpx); `sub` is the user id. Issuer/JWKS URL derived from the publishable key / `CLERK_JWKS_URL` env.
- **Per-user credentials:** the dependency loads the user's Delta key, secret and environment and sets a `ContextVar`. `delta_api._request`/`_sign` read that context first and fall back to the module globals (CLI keeps working). Sync endpoints run in a copied-context threadpool, so concurrent users are isolated.
- **No keys saved:** respond `412 {"detail": "delta_credentials_missing"}`.
- **Storage:** SQLite `data.db` (stdlib `sqlite3`, file mode 600).
  - `credentials(user_id PK, api_key_enc, api_secret_enc, env, updated_at)`
  - `events(id, user_id, ts, action, request, ok, result)`, capped per user.
  - Secrets encrypted with Fernet (`cryptography`) using `CREDENTIAL_ENC_KEY`, generated into `.env` (mode 600) on first run.
- **New endpoints:** `GET /settings/delta` (is_set, env, masked key; never the secret), `PUT /settings/delta` (validates with a cheap authenticated Delta call before saving), `DELETE /settings/delta`.
- `/history` returns only the caller's events; `audited` records `user_id`.
- `/auth/check` validates the token.

## Frontend
- Next.js App Router, `clerkMiddleware` protecting everything except `/sign-in`.
- Routes: `/orders`, `/analysis`, `/history`, `/trade`, `/dashboard`, `/settings`.
- Typed API client + `@tanstack/react-query` (polling, auto-refresh).
- Analysis tab keeps: snapshot history (last 10, prev/next), auto-refresh timer, timeframe chips, tick-to-highlight rows with per-timeframe colours persisted in localStorage.
- Settings: API key, secret (password input, never re-displayed), environment select; Save, Test, Remove.
- Env: `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`, `BACKEND_URL`.

## Errors
- 401 -> redirect to sign-in. 412 -> redirect to Settings with a prompt. Delta errors (502) shown as toasts as today.

## Testing
- Backend (pytest): JWT verification with a fake JWKS, 401/412 paths, per-user isolation of credentials and audit log, encryption round-trip, ContextVar isolation under concurrent requests, Delta calls mocked.
- Frontend: `tsc` typecheck and `next build`; manual run against the real backend via browser tools. Clerk sign-in itself requires a real login by the user.

## Staging
1. Backend multi-user (auth, credential context, storage, settings endpoints, tests).
2. Frontend shell: Clerk login, API proxy, Settings page.
3. Port tabs: Analysis first, then Orders, History, Trade, Dashboard.
4. Remove `backend/static/index.html` once the port is complete.

## Out of scope
Roles/admin, billing, migrating old audit data, multiple Delta accounts per user.
