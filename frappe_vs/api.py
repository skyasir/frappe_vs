"""Whitelisted backend for Frappe VS.

Two modes, chosen automatically by ``developer_mode``:

* **Mode A** (developer_mode ON) — full filesystem IDE. *Built in a later step;*
  its endpoints will be hard-gated by :func:`_require_developer_mode`.
* **Mode B** (developer_mode OFF, but also usable as the safe console anytime) —
  create/edit DB-stored, framework-safe objects. Every write goes through the
  DocType API (``doc.save()`` / ``doc.insert()``) so native validation and
  ``safe_exec`` checks fire. No raw DB writes, never ``ignore_permissions``.

Security rules enforced here (not assumed from the client):

* The whole app is restricted to **System Manager** (:func:`_require_system_manager`),
  on top of per-record ``frappe.has_permission`` checks.
* Mode B only touches object types in :data:`frappe_vs.registry.OBJECT_TYPES`,
  and for field-mode objects only their one resolved code field.
"""

from __future__ import annotations

import os
import secrets
import socket as _socket
import subprocess
import time

import frappe
from frappe import _
from frappe.utils import cint, get_datetime_str

from frappe_vs import __version__
from frappe_vs.registry import (
	OBJECT_TYPES,
	VOLATILE_KEYS,
	get_config,
	resolve_field,
	scaffold,
)


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def _require_user() -> None:
	if frappe.session.user == "Guest":
		raise frappe.PermissionError(_("You must be signed in to use Frappe VS."))


def _require_system_manager() -> None:
	"""The whole app is System Manager-only (the Desk Page enforces this too)."""
	_require_user()
	if "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError(_("Frappe VS is restricted to the System Manager role."))


def _require_developer_mode() -> None:
	"""Security boundary for Mode A (filesystem) endpoints — refuse if OFF.

	Used by the filesystem API (added in a later build step).
	"""
	if not frappe.conf.get("developer_mode"):
		raise frappe.PermissionError(
			_("Filesystem mode is only available when developer_mode is enabled.")
		)


def _label(cfg: dict, doc) -> str:
	label_field = cfg.get("label_field")
	if label_field:
		value = doc.get(label_field)
		if value:
			return value
	return doc.get("name")


def _doc_to_json(doc) -> str:
	data = doc.as_dict()
	for key in list(data.keys()):
		if key in VOLATILE_KEYS:
			data.pop(key, None)
	return frappe.as_json(data, indent=1)


def _match(value, expected) -> bool:
	if isinstance(expected, (list, tuple, set)):
		return value in expected
	return value == expected


# --------------------------------------------------------------------------- #
# Context / registry
# --------------------------------------------------------------------------- #
def _registry() -> list[dict]:
	out: list[dict] = []
	for object_type, cfg in OBJECT_TYPES.items():
		if not frappe.db.exists("DocType", object_type):
			continue
		if not frappe.has_permission(object_type, "read"):
			continue
		out.append(
			{
				"object_type": object_type,
				"edit": cfg["edit"],
				"field": cfg.get("field"),
				"language": cfg.get("language"),
				"icon": cfg["icon"],
				"label_field": cfg.get("label_field"),
				"creatable": bool(cfg.get("creatable")) and bool(frappe.has_permission(object_type, "create")),
				"new_fields": cfg.get("new_fields") or [],
			}
		)
	return out


@frappe.whitelist()
def get_context() -> dict:
	"""Tell the client which mode is active and what Mode B can edit."""
	_require_system_manager()
	developer_mode = bool(frappe.conf.get("developer_mode"))
	out = {
		"developer_mode": developer_mode,
		"active_mode": "A" if developer_mode else "B",
		"object_types": _registry(),
		"user": frappe.session.user,
		"version": __version__,
	}
	if developer_mode:
		out["filesystem"] = {
			"bench_root": _bench_root(),
			"default_root": "apps",
			"editable_extensions": sorted(EDITABLE_EXTS),
		}
		out["terminal"] = {"enabled": True, "cwd": _bench_root()}
	return out


@frappe.whitelist()
def get_registry() -> list[dict]:
	_require_system_manager()
	return _registry()


# --------------------------------------------------------------------------- #
# Mode B — object tree / source
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_records(object_type: str, search: str | None = None, limit: int = 300) -> list[dict]:
	"""List records of an editable object type (permission-filtered)."""
	_require_system_manager()
	cfg = get_config(object_type)
	if not frappe.has_permission(object_type, "read"):
		raise frappe.PermissionError(_("Not permitted to read {0}").format(object_type))

	label_field = cfg.get("label_field")
	# Registry filters are applied in Python (v16 forbids SQL-filtering some of
	# these fields, e.g. `custom`, `report_type`, `standard`).
	post_filters = dict(cfg.get("list_filters") or {})

	fields = ["name", "modified"]
	if label_field and label_field != "name":
		fields.append(label_field)
	for key in post_filters:
		if key not in fields:
			fields.append(key)

	or_filters = None
	if search:
		needle = f"%{search}%"
		or_filters = [["name", "like", needle]]
		if label_field and label_field != "name":
			or_filters.append([label_field, "like", needle])

	records = frappe.get_list(
		object_type,
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit_page_length=cint(limit) or 300,
		ignore_permissions=False,
	)

	out: list[dict] = []
	for r in records:
		if any(not _match(r.get(k), v) for k, v in post_filters.items()):
			continue
		label = r.get(label_field) if label_field else None
		out.append(
			{
				"name": r.get("name"),
				"label": label or r.get("name"),
				"modified": get_datetime_str(r.get("modified")),
			}
		)
	return out


@frappe.whitelist()
def get_source(object_type: str, name: str) -> dict:
	"""Return the editable source (one field, or whole-doc JSON) for a record."""
	_require_system_manager()
	cfg = get_config(object_type)
	if not frappe.has_permission(object_type, "read", doc=name):
		raise frappe.PermissionError(_("Not permitted to read {0} {1}").format(object_type, name))

	doc = frappe.get_doc(object_type, name)
	can_write = bool(frappe.has_permission(object_type, "write", doc=doc))

	if cfg["edit"] == "json":
		code, field, language = _doc_to_json(doc), None, "json"
	else:
		field, language = resolve_field(object_type, doc)
		code = doc.get(field) or ""

	return {
		"object_type": object_type,
		"name": doc.name,
		"edit": cfg["edit"],
		"field": field,
		"language": language,
		"code": code,
		"label": _label(cfg, doc),
		"modified": get_datetime_str(doc.modified),
		"can_write": can_write,
	}


@frappe.whitelist()
def save_source(
	object_type: str, name: str, code: str | None = None, modified: str | None = None
) -> dict:
	"""Persist edited source, re-running the doctype's native validation.

	Field-mode sets one code field; JSON-mode parses and re-applies the document.
	"""
	_require_system_manager()
	cfg = get_config(object_type)
	if not frappe.has_permission(object_type, "write", doc=name):
		raise frappe.PermissionError(_("Not permitted to edit {0} {1}").format(object_type, name))

	doc = frappe.get_doc(object_type, name)

	if modified and get_datetime_str(doc.modified) != get_datetime_str(modified):
		frappe.throw(
			_(
				"{0} was changed (by {1}) after you opened it. "
				"Reload it to get the latest version before saving."
			).format(doc.name, doc.modified_by),
			title=_("File changed on the server"),
		)

	if cfg["edit"] == "json":
		try:
			data = frappe.parse_json(code or "{}")
		except Exception:
			frappe.throw(_("Invalid JSON — could not parse the document."), title=_("JSON Error"))
		if not isinstance(data, dict):
			frappe.throw(_("Expected a JSON object at the top level."), title=_("JSON Error"))
		for key in list(data.keys()):
			if key in VOLATILE_KEYS or key in ("doctype", "name"):
				data.pop(key, None)
		doc.update(data)
	else:
		field, _lang = resolve_field(object_type, doc)
		doc.set(field, code or "")

	doc.save()

	return {
		"object_type": object_type,
		"name": doc.name,
		"modified": get_datetime_str(doc.modified),
		"label": _label(cfg, doc),
	}


@frappe.whitelist()
def create_object(object_type: str, values: str | dict | None = None) -> dict:
	"""Scaffold a new framework-safe object from a template and insert it."""
	_require_system_manager()
	cfg = get_config(object_type)
	if not cfg.get("creatable"):
		frappe.throw(_("{0} cannot be created from Frappe VS.").format(object_type))
	if not frappe.has_permission(object_type, "create"):
		raise frappe.PermissionError(_("Not permitted to create {0}").format(object_type))

	values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
	doc = frappe.get_doc(scaffold(object_type, values))
	doc.insert()  # native validation + safe_exec fire here

	return {
		"object_type": object_type,
		"name": doc.name,
		"label": _label(cfg, doc),
		"modified": get_datetime_str(doc.modified),
	}


# =========================================================================== #
# Mode A — filesystem IDE (developer_mode ONLY)
#
# The security model has three layers, all enforced server-side:
#   1. developer_mode must be ON          -> _require_developer_mode()
#   2. the path must resolve INSIDE the bench root (realpath; symlinks/.. that
#      escape are rejected)                -> _resolve()
#   3. reads/writes are limited to an editable-extension allowlist and a
#      secret-file denylist                -> _require_editable() / _is_secret()
# =========================================================================== #

# Generous allow-list of text/code file extensions that may be opened & saved.
EDITABLE_EXTS = {
	".py", ".pyi", ".js", ".cjs", ".mjs", ".ts", ".jsx", ".tsx", ".vue",
	".json", ".jsonc", ".html", ".htm", ".css", ".scss", ".sass", ".less",
	".md", ".markdown", ".rst", ".txt", ".yaml", ".yml", ".toml", ".cfg",
	".ini", ".conf", ".csv", ".tsv", ".sql", ".xml", ".svg", ".sh", ".bash",
	".zsh", ".env_example", ".j2", ".jinja", ".jinja2", ".po", ".pot",
	".gitignore", ".editorconfig", ".flake8", ".lock",
}
# Extension-less files that are still safe/useful to edit.
EDITABLE_NAMES = {
	"Procfile", "Dockerfile", "Makefile", "LICENSE", "README", "MANIFEST.in",
	".gitignore", ".editorconfig", ".flake8", ".gitkeep", "requirements.txt",
}
# Never serve/modify these even though their extension may be allowed.
SECRET_NAMES = {"site_config.json", "common_site_config.json"}
SECRET_EXTS = {".key", ".pem", ".crt", ".cer", ".p12", ".pfx", ".env"}

MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MB — refuse to stream anything larger
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}

EXT_LANGUAGE = {
	".py": "python", ".pyi": "python", ".js": "javascript", ".cjs": "javascript",
	".mjs": "javascript", ".ts": "typescript", ".jsx": "javascript",
	".tsx": "typescript", ".vue": "html", ".json": "json", ".jsonc": "json",
	".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
	".sass": "scss", ".less": "less", ".md": "markdown", ".markdown": "markdown",
	".rst": "plaintext", ".txt": "plaintext", ".yaml": "yaml", ".yml": "yaml",
	".toml": "ini", ".cfg": "ini", ".ini": "ini", ".conf": "ini", ".csv": "plaintext",
	".tsv": "plaintext", ".sql": "sql", ".xml": "xml", ".svg": "xml", ".sh": "shell",
	".bash": "shell", ".zsh": "shell", ".j2": "html", ".jinja": "html",
	".jinja2": "html", ".po": "plaintext", ".pot": "plaintext",
}


def _bench_root() -> str:
	"""Absolute, symlink-resolved bench directory (parent of ``sites/``)."""
	return os.path.realpath(os.path.join(frappe.local.sites_path, ".."))


def _resolve(path: str | None) -> str:
	"""Resolve a client path to an absolute path *confined to the bench root*.

	``path`` is always treated as relative to the bench root. After resolving
	symlinks and ``..``, anything outside the bench root is rejected.
	"""
	root = _bench_root()
	rel = (path or "").strip().replace("\\", "/").lstrip("/")
	candidate = os.path.realpath(os.path.join(root, rel))
	if candidate != root and not candidate.startswith(root + os.sep):
		raise frappe.PermissionError(_("Path escapes the bench directory: {0}").format(path))
	return candidate


def _rel(abspath: str) -> str:
	return os.path.relpath(abspath, _bench_root())


def _is_secret(abspath: str) -> bool:
	base = os.path.basename(abspath)
	low = base.lower()
	if low in {s.lower() for s in SECRET_NAMES}:
		return True
	if os.path.splitext(low)[1] in SECRET_EXTS:
		return True
	if low.startswith(".env"):
		return True
	if "id_rsa" in low or "id_ed25519" in low or low.endswith(".secret"):
		return True
	return False


def _is_editable(abspath: str) -> bool:
	if _is_secret(abspath):
		return False
	base = os.path.basename(abspath)
	if base in EDITABLE_NAMES:
		return True
	return os.path.splitext(base)[1].lower() in EDITABLE_EXTS


def _require_editable(abspath: str) -> None:
	if _is_secret(abspath):
		frappe.throw(
			_("{0} is a protected file and cannot be opened in Frappe VS.").format(os.path.basename(abspath)),
			frappe.PermissionError,
		)
	if not _is_editable(abspath):
		frappe.throw(
			_("{0} is not an editable file type in Frappe VS.").format(os.path.basename(abspath)),
			frappe.PermissionError,
		)


def _language_for(abspath: str) -> str:
	return EXT_LANGUAGE.get(os.path.splitext(abspath)[1].lower(), "plaintext")


def _fs_guard() -> None:
	"""Both gates for every filesystem endpoint."""
	_require_system_manager()
	_require_developer_mode()


@frappe.whitelist()
def fs_list_dir(path: str = "apps") -> dict:
	"""List a directory inside the bench (dirs first, alpha)."""
	_fs_guard()
	abspath = _resolve(path)
	if not os.path.isdir(abspath):
		frappe.throw(_("Not a directory: {0}").format(path))

	entries = []
	for name in os.listdir(abspath):
		if name in SKIP_DIRS:
			continue
		full = os.path.join(abspath, name)
		is_dir = os.path.isdir(full)
		entries.append(
			{
				"name": name,
				"type": "dir" if is_dir else "file",
				"path": _rel(full),
				"editable": (not is_dir) and _is_editable(full),
				"secret": (not is_dir) and _is_secret(full),
			}
		)
	entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
	return {"path": _rel(abspath), "entries": entries}


@frappe.whitelist()
def fs_read_file(path: str) -> dict:
	"""Read a text file's contents (allowlisted, size-capped, UTF-8)."""
	_fs_guard()
	abspath = _resolve(path)
	if not os.path.isfile(abspath):
		frappe.throw(_("File not found: {0}").format(path))
	_require_editable(abspath)

	size = os.path.getsize(abspath)
	if size > MAX_READ_BYTES:
		frappe.throw(_("File is too large to open ({0} bytes; limit is {1}).").format(size, MAX_READ_BYTES))
	try:
		with open(abspath, encoding="utf-8") as f:
			content = f.read()
	except UnicodeDecodeError:
		frappe.throw(_("This looks like a binary file and cannot be edited as text."))

	return {
		"path": _rel(abspath),
		"name": os.path.basename(abspath),
		"content": content,
		"language": _language_for(abspath),
		"size": size,
		"writable": os.access(abspath, os.W_OK),
	}


@frappe.whitelist()
def fs_write_file(path: str, content: str = "") -> dict:
	"""Save contents to an existing file."""
	_fs_guard()
	abspath = _resolve(path)
	_require_editable(abspath)
	if not os.path.isfile(abspath):
		frappe.throw(_("File does not exist — use Create File instead: {0}").format(path))
	with open(abspath, "w", encoding="utf-8") as f:
		f.write(content or "")
	return {"path": _rel(abspath), "size": os.path.getsize(abspath)}


@frappe.whitelist()
def fs_create_file(path: str, content: str = "") -> dict:
	"""Create a new file (parent must exist; must not already exist)."""
	_fs_guard()
	abspath = _resolve(path)
	_require_editable(abspath)
	if os.path.exists(abspath):
		frappe.throw(_("Already exists: {0}").format(path))
	parent = os.path.dirname(abspath)
	if not os.path.isdir(parent):
		frappe.throw(_("Parent folder does not exist: {0}").format(_rel(parent)))
	with open(abspath, "w", encoding="utf-8") as f:
		f.write(content or "")
	return {"path": _rel(abspath)}


@frappe.whitelist()
def fs_create_folder(path: str) -> dict:
	"""Create a new folder (and any missing parents) inside the bench."""
	_fs_guard()
	abspath = _resolve(path)
	if os.path.exists(abspath):
		frappe.throw(_("Already exists: {0}").format(path))
	os.makedirs(abspath)
	return {"path": _rel(abspath)}


@frappe.whitelist()
def fs_rename(path: str, new_path: str) -> dict:
	"""Rename/move a file or folder within the bench."""
	_fs_guard()
	src = _resolve(path)
	dst = _resolve(new_path)
	if not os.path.exists(src):
		frappe.throw(_("Source not found: {0}").format(path))
	if os.path.exists(dst):
		frappe.throw(_("Target already exists: {0}").format(new_path))
	if os.path.isfile(src):
		_require_editable(src)
		_require_editable(dst)  # the new name must also be an allowed type
	os.rename(src, dst)
	return {"path": _rel(dst)}


@frappe.whitelist()
def fs_delete(path: str) -> dict:
	"""Delete a file or an *empty* folder (non-empty folders are refused)."""
	_fs_guard()
	abspath = _resolve(path)
	if abspath == _bench_root():
		frappe.throw(_("Refusing to delete the bench root."))
	if not os.path.exists(abspath):
		frappe.throw(_("Not found: {0}").format(path))
	if os.path.isdir(abspath):
		if os.listdir(abspath):
			frappe.throw(_("Folder is not empty — delete its contents first."))
		os.rmdir(abspath)
	else:
		if _is_secret(abspath):
			frappe.throw(_("Refusing to delete a protected file."), frappe.PermissionError)
		os.remove(abspath)
	return {"path": _rel(abspath), "deleted": True}


# =========================================================================== #
# Mode A — interactive terminal (developer_mode ONLY)
#
# A real PTY shell is bridged to xterm.js by the standalone
# `frappe_vs.pty_server` (stdlib WebSocket + PTY). This endpoint is the only
# control surface: it is developer_mode + System-Manager gated, ensures the
# server is up (bound to 127.0.0.1), and hands the client a single-use,
# short-lived token to authenticate the WebSocket. The shell is full access by
# design — reachable only on localhost, in developer_mode, by a System Manager.
# =========================================================================== #
TERMINAL_DEFAULT_PORT = 7900


def _terminal_redis():
	import redis  # bundled with frappe

	return redis.from_url(frappe.conf.redis_cache)


def _terminal_port() -> int:
	return int(frappe.conf.get("frappe_vs_terminal_port") or TERMINAL_DEFAULT_PORT)


def _pty_server_running(host: str, port: int) -> bool:
	try:
		with _socket.create_connection((host, port), timeout=0.5):
			return True
	except OSError:
		return False


def _ensure_pty_server(host: str, port: int) -> None:
	if _pty_server_running(host, port):
		return
	python = os.path.join(_bench_root(), "env", "bin", "python")
	logdir = os.path.join(_bench_root(), "logs")
	log = (
		open(os.path.join(logdir, "frappe_vs_pty.log"), "a")
		if os.path.isdir(logdir)
		else subprocess.DEVNULL
	)
	subprocess.Popen(
		[
			python,
			"-m",
			"frappe_vs.pty_server",
			"--bench-root",
			_bench_root(),
			"--redis-url",
			frappe.conf.redis_cache,
			"--host",
			host,
			"--port",
			str(port),
		],
		stdout=log,
		stderr=log,
		start_new_session=True,
		close_fds=True,
	)
	for _ in range(50):
		if _pty_server_running(host, port):
			return
		time.sleep(0.1)
	frappe.throw(_("Could not start the terminal server."))


@frappe.whitelist()
def terminal_start() -> dict:
	"""Ensure the PTY server is up and return a single-use connection token."""
	_fs_guard()  # developer_mode + System Manager
	host = "127.0.0.1"
	port = _terminal_port()
	_ensure_pty_server(host, port)
	token = secrets.token_urlsafe(24)
	_terminal_redis().set(f"fvs_term_token:{token}", frappe.session.user, ex=60)
	return {"host": host, "port": port, "token": token}
