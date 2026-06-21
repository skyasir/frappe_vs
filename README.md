# Frappe VS

A VS Code-like IDE that lives **inside Frappe Desk**, powered by the
[Monaco editor](https://microsoft.github.io/monaco-editor/).

Open it at **`/app/frappe_vs`** (System Manager only).

## What it is

Frappe VS is a Frappe custom app (no React build, no Node toolchain required).
A single Desk Page mounts Monaco and talks to a small set of whitelisted Python
methods in [`frappe_vs/api.py`](frappe_vs/api.py).

## Two modes (switched automatically by `developer_mode`)

| Mode | When | What it does | Status |
|------|------|--------------|--------|
| **A** | `developer_mode` **ON** | Full filesystem IDE over the bench (browse/edit/create files, terminal, git diff). | _planned (steps 3–5)_ |
| **B** | `developer_mode` **OFF** (and as a safe console anytime) | Create/edit DB-stored, framework-safe objects through the DocType API. | **shipped** |

The active mode is detected server-side (`frappe.conf.developer_mode`) and
returned by `get_context`. The filesystem endpoints (Mode A) are hard-gated by a
server-side `developer_mode` check — the client is never trusted.

## Mode B — safe object console (shipped)

Create and edit these objects in Monaco. Every save goes through `doc.save()` /
`doc.insert()`, so **native validation and `safe_exec` checks always fire** — no
raw DB writes, never `ignore_permissions`.

| Object type | Editor | Language |
|-------------|--------|----------|
| Server Script | field `script` | python |
| Client Script | field `script` | javascript |
| Print Format (custom) | field `html` | html |
| Web Template | field `template` | html |
| Notification | field `message` | html |
| Web Page | field `main_section_html` | html |
| Report (Query / Script) | `query` / `report_script` (by type) | sql / python |
| Custom DocType | whole document | json |
| Page (custom) | whole document | json |
| Custom Field | whole document | json |
| Property Setter | whole document | json |

The mapping lives in [`frappe_vs/registry.py`](frappe_vs/registry.py), which is
also the allow-list: any object type not listed is refused by every endpoint.

- **Explorer** groups objects by type, lazy-loaded and permission-filtered.
- **“New …”** scaffolds any creatable object from a template (a dialog built
  from each type's required fields) and opens it.
- **Tabs** with dirty dots, **Ctrl/Cmd+S**, light/dark theme.
- Everything **permission-gated** per record (`frappe.has_permission`) on top of
  the app-wide System Manager restriction.

### Backend (whitelisted, all System-Manager + permission gated)

`get_context` · `get_registry` · `list_records` · `get_source` · `save_source`
· `create_object`

## Monaco loading

Loaded through its AMD loader from a CDN (jsDelivr) with a **self-host fallback**
served at `/assets/frappe_vs/monaco/`. See
[`frappe_vs/public/monaco/README.md`](frappe_vs/public/monaco/README.md) to
vendor Monaco locally (run `./fetch_monaco.sh`) for offline / air-gapped use.

## Build order

1. ✅ App scaffold + Desk Page + Monaco mount + mode detection.
2. ✅ Mode B — safe object editor + “New” scaffolding (works on any site).
3. ⏳ Mode A file tree + read/edit/save/create/delete, `developer_mode`-gated,
   path-confined to the bench root.
4. ⏳ Integrated terminal (xterm.js over a websocket).
5. ⏳ Git diff + find-in-files + remaining VS Code polish.

## License

MIT
