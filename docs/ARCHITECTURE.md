# Architecture (DDD-light)

OnPrem AI Gateway splits into a few **bounded contexts**. Each has one job; names
match that job.

## Context map

```
                    ┌─────────────────────────────────────┐
  Browser (cookie)  │  app/web/          Web UI           │
                    │  /me /keys /login /settings …       │
                    └──────────────┬──────────────────────┘
                                   │ session + HTML
                    ┌──────────────▼──────────────────────┐
  API client (key)  │  app/main.py + app/auth/   API      │
                    │  /v1/chat/completions …             │
                    └──────────────┬──────────────────────┘
                                   │ authorize + proxy
                    ┌──────────────▼──────────────────────┐
                    │  app/data/         Persistence      │
                    │  SQLite, models, catalog, grants    │
                    └─────────────────────────────────────┘
```

| Path | Context | Responsibility |
|------|---------|------------------|
| `app/web/` | **Web UI** | Browser UX; `onprem_session` cookie |
| `app/web/portal/` | **Self-service** | `/me`, `/keys`, `/usage` — every user |
| `app/web/platform/` | **Ops** | `/`, `/users`, `/settings` — `is_platform_admin` only |
| `app/web/public/` | **Auth pages** | `/login`, `/logout` |
| `app/auth/` | **API auth** | `X-Api-Key`, rate limits, `/v1/auth/check` |
| `app/data/` | **Data** | DB, sources, catalog, usage events |

Deploy: **onprem-auth** runs `app/main.py`. **onprem-api** (nginx) serves `/v1/*`.

## Roles (not backdoors)

| Role | Flag | Access |
|------|------|--------|
| **Platform admin** | `is_platform_admin=True` | Ops sidebar, user management, backends |
| **User** | `is_platform_admin=False` | Self-service only |

Every web account is a **`WebUser`** (`web_users` table). The flag controls ops —
there is no hidden “everyone is admin” path.

Gates: `app/web/session.py` — `require_user` vs `require_platform_admin`.

## Fresh install

Pre-release: `onprem.db`, `web_users`, volume `onprem-auth-data`, env `ONPREM_API_PORT` only.
No upgrade migrations from old product names.

## Package layout

```
app/web/
  shared.py           # shared helpers + templates
  routes.py           # aggregates public + portal + platform routers
  session.py          # cookie session + role gates
  accounts.py         # register, profile, SMTP (platform)
  public/auth.py
  portal/me.py keys.py teams.py usage.py
  platform/setup.py dashboard.py users.py models.py services.py settings.py
```
