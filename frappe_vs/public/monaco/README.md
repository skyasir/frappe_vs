# Self-hosted Monaco (offline fallback)

Frappe VS loads the Monaco editor from a CDN by default. If the CDN is
unreachable (offline / air-gapped install), it falls back to a local copy
served from this folder at **`/assets/frappe_vs/monaco/vs/loader.js`**.

The local copy is **not committed** (see `.gitignore`). To vendor it, run the
helper from the app root:

```bash
cd apps/frappe_vs
./fetch_monaco.sh        # downloads the pinned version into this folder
```

This populates `monaco/vs/` so that:

- `/assets/frappe_vs/monaco/vs/loader.js`
- `/assets/frappe_vs/monaco/vs/editor/editor.main.js`
- `/assets/frappe_vs/monaco/vs/base/worker/workerMain.js`

…all resolve locally. The pinned version must match `FVS_MONACO_VERSION` in
`frappe_vs/page/frappe_vs/frappe_vs.js` (currently **0.52.2**).

After vendoring, run `bench build --app frappe_vs` (or restart the bench) so the
new static files are picked up.
