# Delta Console

A multi-user trading console for Delta Exchange India. Each signed-in user manages their
own positions, orders, and analysis against their own Delta Exchange API credentials.

- **Frontend:** Next.js (App Router), with Clerk for sign-in.
- **Backend:** FastAPI, verifying Clerk-issued JWTs and storing each user's Delta API
  credentials encrypted (Fernet) in SQLite.

There is no shared "operator" API key. After signing in with Clerk, each user enters their
own Delta Exchange API key and secret on the **Settings** page; the backend uses those
credentials (and only those) for every request made on that user's behalf.

## Running locally

### Backend (FastAPI)

```bash
cd backend
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

### Frontend (Next.js)

```bash
cd frontend
npm run dev
```

## Required environment files

### `backend/.env`

- `CLERK_PUBLISHABLE_KEY` — used to derive the Clerk JWKS issuer for verifying session
  tokens.
- Backend secrets (e.g. the Fernet key used to encrypt stored Delta credentials) are
  auto-generated on first run and stored locally; do not commit or print their values.

### `frontend/.env.local`

- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- `CLERK_SECRET_KEY`
- `BACKEND_URL` — base URL of the FastAPI backend (e.g. `http://127.0.0.1:8000`)

None of these values should ever be printed or committed to version control.

## Delta Exchange API keys

Delta API keys are per-user, not shared. After signing in, go to **Settings** and save
your own Delta Exchange API key/secret. The backend stores it encrypted and uses it only
for requests made in your session. There is no fallback to a shared/operator key — if a
user hasn't saved credentials, their requests to Delta-backed endpoints will fail until
they do.

## Running tests

### Backend

```bash
cd backend
.venv/bin/python -m pytest
```

### Frontend

```bash
cd frontend
npx vitest run
```
