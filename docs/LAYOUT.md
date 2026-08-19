# Web UI layout modes

The OnPrem AI Gateway web UI uses **three layout modes**. Each page uses exactly one mode.
Do not mix scroll owners on the same page (no page scroll + nested panel scroll unless the
inner region is intentional, e.g. a model picker).

## Mode A — Browse (default)

**CSS:** `.content` only (no `content--fill`)

**Behaviour:** The main column scrolls vertically. Page head, subnavs, cards, and tables
move with the scroll.

**Use when:**

- Dashboards and overviews (multiple independent sections)
- List / log / analytics pages (tables, filters, charts stacked)
- Settings with several panels per tab
- Short read-only or low-interaction forms

**Scroll rules:**

| Element | Scroll |
|---------|--------|
| Main column | Vertical |
| `.table-wrap` | Horizontal when needed |
| Charts / cards | Fixed height, no inner scroll |

**Pages:** `/me`, `/usage`, `/usage/daily`, `/keys`, `/users`, `/teams`, `/models`,
`/services`, `/` (platform ops dashboard), `/audit`, `/settings/*`, `/smtp`, `/alerts`,
`/setup/done`, `/legal/*` (when signed in)

## Mode B — Task (`content--fill`)

**CSS:** `{% block content_class %} content--fill{% endblock %}` on the page, plus
`panel--fill` and `wizard-form wizard-form--fill` (or `profile-form profile-form--fill`
for compact account forms).

**Behaviour:** Sidebar and page head stay fixed. One primary panel fills the remaining
viewport height. Sticky actions sit at the bottom of that panel.

**Use when:**

- Single focused task (wizard step, profile, grant editor)
- A large interactive widget is the main work area (model picker)
- User should always see save/cancel without scrolling the whole app shell

**Structure:**

```
page-head (flex-shrink: 0)
[optional banner / flash]
panel--fill
  form.wizard-form--fill
    wizard-top        — meta, sources, hints (flex-shrink: 0)
    wizard-grow       — model picker + optional extra fields (overflow-y: auto)
    wizard-actions--sticky — primary CTA
```

**Pages:** `/setup/*` (wizard), `/account`, `/privacy`, `/keys/new`, `/teams/new`,
`/teams/{id}`, `/users/{id}/grant`

Existing API keys are edited **inline** on `/keys` (Browse), not as a Task page.

Compact account pages (`/account`, `/privacy`) use Task even without a model picker:
they are short, focused, and should occupy the remaining viewport instead of a
thin strip plus empty space.

## Mode C — Auth

**CSS:** `login-wrap` / centered card (no app sidebar layout)

**Pages:** `/login`, `/forgot`, `/reset`, `/register`, `/legal/*` (when signed out)

## Expandable (`<details>`)

Use **only** for secondary inline actions (e.g. “Edit” on a user table row). Never for
primary page content (profile, settings, main forms).

## CI

`tests/test_web_layout_convention.py` asserts which templates use `content--fill` and
which must not, so layout drift is caught in CI.
