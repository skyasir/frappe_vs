/**
 * Frappe VS — a VS Code-like editor inside Frappe Desk, powered by Monaco.
 *
 * Phase 1: explorer tree + open/edit/save, tabs with dirty dots, Ctrl/Cmd+S,
 * light/dark theme toggle. All persistence goes through the whitelisted methods
 * in frappe_vs/api.py, which enforce permissions and native validation.
 *
 * No build step: this file (and frappe_vs.css) are read off disk and inlined by
 * Frappe's Page loader at runtime.
 */

frappe.provide("frappe.frappe_vs");

// Pinned Monaco version. Loaded via its AMD loader from a CDN, with a self-host
// fallback served from this app's public/ folder (see monaco/README.md).
const FVS_MONACO_VERSION = "0.52.2";
const FVS_MONACO_CDN = `https://cdn.jsdelivr.net/npm/monaco-editor@${FVS_MONACO_VERSION}/min/`;
const FVS_MONACO_LOCAL = "/assets/frappe_vs/monaco/";

frappe.pages["frappe_vs"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Frappe VS"),
		single_column: true,
	});
	wrapper.frappe_vs = new frappe.frappe_vs.Workbench(page, wrapper);
};

frappe.pages["frappe_vs"].on_page_show = function (wrapper) {
	wrapper.frappe_vs && wrapper.frappe_vs.on_show();
};

/* ------------------------------------------------------------------ *
 * Monaco loader (memoised across page visits)
 * ------------------------------------------------------------------ */
function fvs_load_monaco() {
	if (window.__fvs_monaco) return Promise.resolve(window.__fvs_monaco);
	if (window.__fvs_monaco_promise) return window.__fvs_monaco_promise;

	window.__fvs_monaco_promise = new Promise((resolve, reject) => {
		const try_load = (base, on_fail) => {
			fvs_inject_script(base + "vs/loader.js")
				.then(() => fvs_boot_monaco(base, resolve, reject))
				.catch(on_fail);
		};
		// Prefer the CDN; on any network/script error, fall back to self-hosted.
		try_load(FVS_MONACO_CDN, () => {
			try_load(FVS_MONACO_LOCAL, () =>
				reject(new Error("Could not load Monaco from CDN or self-host fallback"))
			);
		});
	});
	return window.__fvs_monaco_promise;
}

function fvs_inject_script(src) {
	return new Promise((resolve, reject) => {
		const el = document.createElement("script");
		el.src = src;
		el.onload = () => resolve();
		el.onerror = () => {
			el.remove();
			reject(new Error("script load failed: " + src));
		};
		document.head.appendChild(el);
	});
}

function fvs_boot_monaco(base, resolve, reject) {
	try {
		// Cross-origin web workers: load workerMain via a same-origin blob that
		// importScripts the (possibly cross-origin) worker bundle.
		window.MonacoEnvironment = {
			getWorkerUrl: function () {
				const code =
					`self.MonacoEnvironment = { baseUrl: "${base}" };\n` +
					`importScripts("${base}vs/base/worker/workerMain.js");`;
				return URL.createObjectURL(new Blob([code], { type: "text/javascript" }));
			},
		};
		// `require` here is Monaco's AMD loader, installed by loader.js above.
		window.require.config({ paths: { vs: base + "vs" } });
		window.require(["vs/editor/editor.main"], function () {
			window.__fvs_monaco = window.monaco;
			resolve(window.monaco);
		});
	} catch (e) {
		reject(e);
	}
}

/* ------------------------------------------------------------------ *
 * Workbench
 * ------------------------------------------------------------------ */
frappe.frappe_vs.Workbench = class Workbench {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.registry = [];
		this.groups = {}; // doctype -> { loaded, records, expanded }
		this.files = new Map(); // key -> file state
		this.view_states = {}; // key -> monaco view state
		this.active_key = null;
		this.editor = null;
		this.theme = this.initial_theme();

		this.make_layout();
		this.make_actions();
		this.bind_global_keys();
		this.boot();
	}

	initial_theme() {
		const saved = localStorage.getItem("fvs_theme");
		if (saved) return saved;
		// Follow Desk's dark mode if no explicit choice yet.
		const mode = document.documentElement.getAttribute("data-theme");
		return mode === "dark" ? "vs-dark" : "vs";
	}

	make_layout() {
		this.$root = $(`
			<div class="fvs-root ${this.theme === "vs-dark" ? "fvs-dark" : ""}">
				<div class="fvs-sidebar">
					<div class="fvs-sidebar-head">
						<span class="fvs-sidebar-title">${__("Explorer")}</span>
						<span class="fvs-sidebar-actions">
							<button class="fvs-icon-btn fvs-new" title="${__("New object")}">＋</button>
							<button class="fvs-icon-btn fvs-refresh" title="${__("Reload tree")}">⟳</button>
						</span>
					</div>
					<div class="fvs-search">
						<input type="text" class="fvs-search-input"
							placeholder="${__("Filter open files…")}" spellcheck="false" />
					</div>
					<div class="fvs-tree"></div>
				</div>
				<div class="fvs-main">
					<div class="fvs-banner" style="display:none"></div>
					<div class="fvs-tabbar"></div>
					<div class="fvs-editor-wrap">
						<div class="fvs-editor"></div>
						<div class="fvs-welcome">
							<div class="fvs-welcome-inner">
								<div class="fvs-welcome-logo">&lt;/&gt;</div>
								<h2>${__("Frappe VS")}</h2>
								<p>${__("Open a document from the Explorer to start editing.")}</p>
								<p class="fvs-muted">${__("Press Ctrl/Cmd + S to save. Saving re-runs the document's own validation.")}</p>
							</div>
						</div>
					</div>
					<div class="fvs-statusbar">
						<span class="fvs-status-left"></span>
						<span class="fvs-status-right"></span>
					</div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.$tree = this.$root.find(".fvs-tree");
		this.$banner = this.$root.find(".fvs-banner");
		this.$tabbar = this.$root.find(".fvs-tabbar");
		this.$editor = this.$root.find(".fvs-editor");
		this.$welcome = this.$root.find(".fvs-welcome");
		this.$status_left = this.$root.find(".fvs-status-left");
		this.$status_right = this.$root.find(".fvs-status-right");

		this.$root.find(".fvs-refresh").on("click", () => this.reload_tree());
		this.$root.find(".fvs-new").on("click", () => this.open_new());
		this.$root.find(".fvs-search-input").on("input", (e) =>
			this.filter_tree($(e.currentTarget).val())
		);

		// Keep the workbench filling the viewport.
		this._resize = frappe.utils.debounce(() => this.resize(), 80);
		$(window).on("resize.fvs", this._resize);
		this.resize();
	}

	make_actions() {
		this.page.set_primary_action(
			__("Save"),
			() => this.save_active(),
			"es-line-save"
		);
		this.page.add_inner_button(__("New…"), () => this.open_new());
		this.$theme_btn = this.page.add_inner_button(__("Toggle Theme"), () =>
			this.toggle_theme()
		);
		this.page.add_inner_button(__("Reload Tree"), () => this.reload_tree());
	}

	bind_global_keys() {
		// Catch Ctrl/Cmd+S even when focus is outside Monaco, and stop the
		// browser's own save dialog.
		this.$root.on("keydown", (e) => {
			const mod = e.ctrlKey || e.metaKey;
			if (mod && (e.key === "s" || e.key === "S")) {
				e.preventDefault();
				this.save_active();
			}
		});
	}

	resize() {
		const top = this.$root.offset() ? this.$root.offset().top : 0;
		const h = Math.max(360, window.innerHeight - top - 12);
		this.$root.css("height", h + "px");
		this.editor && this.editor.layout();
	}

	on_show() {
		this.resize();
		this.editor && this.editor.layout();
	}

	async boot() {
		try {
			this.context = await frappe.xcall("frappe_vs.api.get_context");
			this.registry = this.context.object_types;
			this.developer_mode = this.context.developer_mode;
		} catch (e) {
			this.set_status_left(__("Failed to load context"));
			return;
		}
		this.render_banner();
		this.render_tree();
		try {
			await this.init_editor();
		} catch (e) {
			console.error(e);
			frappe.msgprint({
				title: __("Monaco failed to load"),
				message: __(
					"The Monaco editor could not be loaded from the CDN or the self-hosted fallback. Check your network or vendor Monaco locally."
				),
				indicator: "red",
			});
		}
	}

	async init_editor() {
		const monaco = await fvs_load_monaco();
		this.monaco = monaco;
		this.editor = monaco.editor.create(this.$editor.get(0), {
			value: "",
			language: "plaintext",
			theme: this.theme,
			automaticLayout: false, // we drive layout() ourselves on resize
			minimap: { enabled: true },
			fontSize: 13,
			scrollBeyondLastLine: false,
			renderWhitespace: "selection",
			tabSize: 4,
			model: null,
		});

		// Ctrl/Cmd+S inside the editor.
		this.editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () =>
			this.save_active()
		);
		this.editor.onDidChangeCursorPosition(() => this.update_status());
		this.set_status_left(__("Ready"));
		this.update_status();
	}

	/* ----------------------------- Explorer ----------------------------- */

	render_tree() {
		this.$tree.empty();
		if (!this.registry.length) {
			this.$tree.append(
				`<div class="fvs-tree-empty">${__("No editable doctypes available.")}</div>`
			);
			return;
		}
		this.registry.forEach((entry) => {
			const dt = entry.object_type;
			if (!this.groups[dt]) this.groups[dt] = { loaded: false, records: [], expanded: false };
			const $group = $(`
				<div class="fvs-group" data-doctype="${frappe.utils.escape_html(dt)}">
					<div class="fvs-group-head">
						<span class="fvs-chevron">▸</span>
						<span class="fvs-group-icon">${frappe.utils.escape_html(entry.icon || "")}</span>
						<span class="fvs-group-label">${frappe.utils.escape_html(dt)}</span>
						<span class="fvs-group-count"></span>
					</div>
					<div class="fvs-group-body"></div>
				</div>
			`);
			$group.find(".fvs-group-head").on("click", () => this.toggle_group(dt, $group));
			this.$tree.append($group);
		});
	}

	registry_entry(object_type) {
		return (this.registry || []).find((e) => e.object_type === object_type);
	}

	async toggle_group(doctype, $group) {
		const g = this.groups[doctype];
		g.expanded = !g.expanded;
		$group.toggleClass("fvs-open", g.expanded);
		$group.find(".fvs-chevron").text(g.expanded ? "▾" : "▸");
		if (g.expanded && !g.loaded) {
			await this.load_group(doctype, $group);
		}
	}

	async load_group(doctype, $group) {
		const $body = $group.find(".fvs-group-body");
		$body.html(`<div class="fvs-loading">${__("Loading…")}</div>`);
		try {
			const records = await frappe.xcall("frappe_vs.api.list_records", {
				object_type: doctype,
			});
			this.groups[doctype].records = records;
			this.groups[doctype].loaded = true;
			this.render_group_items(doctype, $group);
		} catch (e) {
			$body.html(`<div class="fvs-tree-empty">${__("Could not load records")}</div>`);
		}
	}

	render_group_items(doctype, $group) {
		const g = this.groups[doctype];
		const $body = $group.find(".fvs-group-body").empty();
		$group.find(".fvs-group-count").text(g.records.length ? g.records.length : "");
		if (!g.records.length) {
			$body.append(`<div class="fvs-tree-empty">${__("No records")}</div>`);
			return;
		}
		g.records.forEach((r) => {
			const key = this.file_key(doctype, r.name);
			const $row = $(`
				<div class="fvs-file" data-key="${frappe.utils.escape_html(key)}"
						title="${frappe.utils.escape_html(r.name)}">
					<span class="fvs-file-dot"></span>
					<span class="fvs-file-name">${frappe.utils.escape_html(r.label)}</span>
				</div>
			`);
			$row.on("click", () => this.open_file(doctype, r.name));
			$body.append($row);
		});
		this.refresh_tree_active();
	}

	filter_tree(term) {
		term = (term || "").toLowerCase();
		this.$tree.find(".fvs-file").each((_, el) => {
			const $el = $(el);
			const text = $el.find(".fvs-file-name").text().toLowerCase();
			$el.toggle(!term || text.indexOf(term) !== -1);
		});
	}

	async reload_tree() {
		this.groups = {};
		this.registry = await frappe.xcall("frappe_vs.api.get_registry");
		this.render_tree();
		frappe.show_alert({ message: __("Explorer reloaded"), indicator: "blue" });
	}

	/* ------------------------ Mode banner / New ------------------------- */

	render_banner() {
		if (this.developer_mode) {
			// Mode A (filesystem IDE) is not built yet; Mode B is what runs now.
			this.$banner
				.html(
					`<span class="fvs-banner-tag">${__("developer_mode ON")}</span> ` +
						__(
							"Full filesystem IDE (Mode A) arrives in the next build step — showing the safe object console (Mode B)."
						)
				)
				.css("display", "")
				.removeClass("fvs-banner-safe")
				.addClass("fvs-banner-dev");
		} else {
			this.$banner
				.html(
					`<span class="fvs-banner-tag">${__("Mode B")}</span> ` +
						__("Safe object console — edits go through the DocType API with full validation.")
				)
				.css("display", "")
				.removeClass("fvs-banner-dev")
				.addClass("fvs-banner-safe");
		}
	}

	/** Reveal a group, (re)load it, then open the record. */
	async reveal_and_open(object_type, name) {
		const $group = this.$tree.find(`.fvs-group[data-doctype="${object_type}"]`);
		if ($group.length) {
			const g =
				this.groups[object_type] ||
				(this.groups[object_type] = { loaded: false, records: [], expanded: false });
			g.expanded = true;
			g.loaded = false; // force reload so the new record shows
			$group.addClass("fvs-open");
			$group.find(".fvs-chevron").text("▾");
			await this.load_group(object_type, $group);
		}
		this.open_file(object_type, name);
	}

	open_new() {
		const creatable = (this.registry || []).filter((e) => e.creatable);
		if (!creatable.length) {
			frappe.show_alert({ message: __("Nothing here can be created."), indicator: "orange" });
			return;
		}
		const picker = new frappe.ui.Dialog({
			title: __("New Object"),
			fields: [
				{
					fieldname: "object_type",
					label: __("What do you want to create?"),
					fieldtype: "Select",
					options: creatable.map((e) => e.object_type).join("\n"),
					default: creatable[0].object_type,
					reqd: 1,
				},
			],
			primary_action_label: __("Continue"),
			primary_action: (v) => {
				picker.hide();
				this.open_new_dialog(this.registry_entry(v.object_type));
			},
		});
		picker.show();
	}

	open_new_dialog(entry) {
		if (!entry) return;
		const fields = (entry.new_fields || []).map((f) => ({
			fieldname: f.fieldname,
			label: f.label,
			fieldtype: f.fieldtype,
			options: f.options,
			reqd: f.reqd,
			default: f.default,
		}));
		const dialog = new frappe.ui.Dialog({
			title: __("New {0}", [entry.object_type]),
			fields: fields.length
				? fields
				: [{ fieldtype: "HTML", options: `<p>${__("No parameters needed.")}</p>` }],
			primary_action_label: __("Create"),
			primary_action: (values) => {
				frappe
					.call({
						method: "frappe_vs.api.create_object",
						args: { object_type: entry.object_type, values: JSON.stringify(values) },
						freeze: true,
						freeze_message: __("Creating {0}…", [entry.object_type]),
					})
					.then((r) => {
						if (!r || !r.message) return; // server error already shown
						dialog.hide();
						frappe.show_alert({
							message: __("Created {0}", [r.message.label || r.message.name]),
							indicator: "green",
						});
						this.reveal_and_open(entry.object_type, r.message.name);
					});
			},
		});
		dialog.show();
	}

	/* ------------------------------ Files ------------------------------- */

	file_key(doctype, name) {
		return doctype + "::" + name;
	}

	async open_file(doctype, name) {
		const key = this.file_key(doctype, name);
		if (this.files.has(key)) {
			this.activate(key);
			return;
		}
		let data;
		try {
			data = await frappe.xcall("frappe_vs.api.get_source", {
				object_type: doctype,
				name,
			});
		} catch (e) {
			return; // frappe already surfaced the error
		}
		await fvs_load_monaco();
		const model = this.monaco.editor.createModel(data.code, data.language);
		const file = {
			key,
			doctype,
			name,
			edit: data.edit,
			field: data.field,
			language: data.language,
			label: data.label,
			modified: data.modified,
			can_write: data.can_write,
			baseline: data.code,
			dirty: false,
			model,
		};
		model.onDidChangeContent(() => this.on_model_change(key));
		this.files.set(key, file);
		this.activate(key);
	}

	on_model_change(key) {
		const file = this.files.get(key);
		if (!file) return;
		const dirty = file.model.getValue() !== file.baseline;
		if (dirty !== file.dirty) {
			file.dirty = dirty;
			this.render_tabs();
			this.refresh_tree_active();
		}
	}

	activate(key) {
		const file = this.files.get(key);
		if (!file || !this.editor) return;

		// Stash the outgoing view state so cursor/scroll survive tab switches.
		if (this.active_key && this.files.has(this.active_key)) {
			this.view_states[this.active_key] = this.editor.saveViewState();
		}
		this.active_key = key;
		this.editor.setModel(file.model);
		this.editor.updateOptions({ readOnly: !file.can_write });
		if (this.view_states[key]) this.editor.restoreViewState(this.view_states[key]);
		this.editor.focus();
		this.$welcome.hide();
		this.render_tabs();
		this.refresh_tree_active();
		this.update_status();
	}

	close_file(key) {
		const file = this.files.get(key);
		if (!file) return;
		const proceed = () => {
			file.model.dispose();
			this.files.delete(key);
			delete this.view_states[key];
			if (this.active_key === key) {
				this.active_key = null;
				const next = Array.from(this.files.keys()).pop();
				if (next) {
					this.activate(next);
				} else {
					this.editor.setModel(null);
					this.$welcome.show();
					this.render_tabs();
					this.refresh_tree_active();
					this.update_status();
				}
			} else {
				this.render_tabs();
				this.refresh_tree_active();
			}
		};
		if (file.dirty) {
			frappe.confirm(
				__("{0} has unsaved changes. Close without saving?", [file.label]),
				proceed
			);
		} else {
			proceed();
		}
	}

	/* ------------------------------ Tabs -------------------------------- */

	render_tabs() {
		this.$tabbar.empty();
		this.files.forEach((file, key) => {
			const active = key === this.active_key;
			const $tab = $(`
				<div class="fvs-tab ${active ? "fvs-tab-active" : ""}" data-key="${frappe.utils.escape_html(key)}">
					<span class="fvs-tab-icon">${file.can_write ? "" : "🔒"}</span>
					<span class="fvs-tab-label">${frappe.utils.escape_html(file.label)}</span>
					<span class="fvs-tab-doctype">${frappe.utils.escape_html(file.doctype)}</span>
					<span class="fvs-tab-close" title="${__("Close")}">${file.dirty ? "●" : "✕"}</span>
				</div>
			`);
			$tab.on("click", (e) => {
				if ($(e.target).hasClass("fvs-tab-close")) {
					this.close_file(key);
				} else {
					this.activate(key);
				}
			});
			// Hovering the dirty dot reveals a close affordance.
			$tab.find(".fvs-tab-close").on("mouseenter", function () {
				if (file.dirty) $(this).text("✕");
			});
			$tab.find(".fvs-tab-close").on("mouseleave", function () {
				if (file.dirty) $(this).text("●");
			});
			this.$tabbar.append($tab);
		});
	}

	refresh_tree_active() {
		this.$tree.find(".fvs-file").each((_, el) => {
			const $el = $(el);
			const key = $el.data("key");
			const file = this.files.get(key);
			$el.toggleClass("fvs-file-open", this.files.has(key));
			$el.toggleClass("fvs-file-active", key === this.active_key);
			$el.find(".fvs-file-dot").toggleClass("on", !!(file && file.dirty));
		});
	}

	/* ------------------------------ Save -------------------------------- */

	save_active() {
		const file = this.files.get(this.active_key);
		if (!file) return;
		if (!file.can_write) {
			frappe.show_alert({
				message: __("{0} is read-only for you.", [file.label]),
				indicator: "orange",
			});
			return;
		}
		if (!file.dirty) {
			frappe.show_alert({ message: __("Nothing to save"), indicator: "blue" });
			return;
		}
		const code = file.model.getValue();
		frappe
			.call({
				method: "frappe_vs.api.save_source",
				args: {
					object_type: file.doctype,
					name: file.name,
					code: code,
					modified: file.modified,
				},
				freeze: true,
				freeze_message: __("Saving {0}…", [file.label]),
			})
			.then((r) => {
				if (!r || !r.message) return; // server error already shown
				file.baseline = code;
				file.modified = r.message.modified;
				file.label = r.message.label || file.label;
				file.dirty = false;
				this.render_tabs();
				this.refresh_tree_active();
				this.update_status();
				frappe.show_alert({
					message: __("Saved {0}", [file.label]),
					indicator: "green",
				});
			});
		// On validation/permission failure frappe.call surfaces the message and
		// the file stays dirty, so nothing is silently lost.
	}

	/* ------------------------------ Theme ------------------------------- */

	toggle_theme() {
		this.theme = this.theme === "vs-dark" ? "vs" : "vs-dark";
		this.apply_theme();
		localStorage.setItem("fvs_theme", this.theme);
	}

	apply_theme() {
		this.$root.toggleClass("fvs-dark", this.theme === "vs-dark");
		this.monaco && this.monaco.editor.setTheme(this.theme);
	}

	/* --------------------------- Status bar ----------------------------- */

	set_status_left(text) {
		this.$status_left.text(text);
	}

	update_status() {
		const file = this.files.get(this.active_key);
		if (!file) {
			this.set_status_left(this.editor ? __("Ready") : __("Loading Monaco…"));
			this.$status_right.text("");
			return;
		}
		const pos = this.editor.getPosition();
		const ln = pos ? pos.lineNumber : 1;
		const col = pos ? pos.column : 1;
		const flags = [file.language.toUpperCase()];
		if (!file.can_write) flags.push(__("read-only"));
		if (file.dirty) flags.push(__("unsaved"));
		this.set_status_left(`${file.doctype} › ${file.name}`);
		this.$status_right.text(`Ln ${ln}, Col ${col}    ${flags.join("  ·  ")}`);
	}
};
