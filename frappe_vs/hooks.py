from . import __version__ as app_version  # noqa: F401

app_name = "frappe_vs"
app_title = "Frappe VS"
app_publisher = "Frappe VS"
app_description = "A VS Code-like IDE inside Frappe Desk, powered by the Monaco editor."
app_email = "support@example.com"
app_license = "mit"

# Page assets (frappe_vs.js / frappe_vs.css) are loaded directly from the page
# folder by Frappe at runtime, so we deliberately do NOT register any global
# app_include_js / app_include_css here. That keeps all of Frappe VS's CSS (the
# .fvs-* rules) scoped to its own page and prevents it from leaking into the
# rest of Desk.
