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
	return {
		"developer_mode": developer_mode,
		"active_mode": "A" if developer_mode else "B",
		"object_types": _registry(),
		"user": frappe.session.user,
		"version": __version__,
	}


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
