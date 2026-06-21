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
							<button class="fvs-icon-btn fvs-new" title="${__("New")}">＋</button>
							<button class="fvs-icon-btn fvs-refresh" title="${__("Reload")}">⟳</button>
						</span>
					</div>
					<div class="fvs-explorer-tabs" style="display:none"></div>
					<div class="fvs-fs-roots" style="display:none"></div>
					<div class="fvs-search">
						<input type="text" class="fvs-search-input"
							placeholder="${__("Filter…")}" spellcheck="false" />
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
		this.$explorer_tabs = this.$root.find(".fvs-explorer-tabs");
		this.$fs_roots = this.$root.find(".fvs-fs-roots");
		this.$tabbar = this.$root.find(".fvs-tabbar");
		this.$editor = this.$root.find(".fvs-editor");
		this.$welcome = this.$root.find(".fvs-welcome");
		this.$status_left = this.$root.find(".fvs-status-left");
		this.$status_right = this.$root.find(".fvs-status-right");

		this.$root.find(".fvs-refresh").on("click", () => this.reload_explorer());
		this.$root.find(".fvs-new").on("click", () => this.on_new_click());
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
		this.page.add_inner_button(__("New…"), () => this.on_new_click());
		this.$theme_btn = this.page.add_inner_button(__("Toggle Theme"), () =>
			this.toggle_theme()
		);
		this.page.add_inner_button(__("Reload"), () => this.reload_explorer());
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
			this.fs = this.context.filesystem || null;
		} catch (e) {
			this.set_status_left(__("Failed to load context"));
			return;
		}
		// developer_mode ON -> default to the filesystem IDE (Mode A).
		this.explorer_mode = this.developer_mode ? "fs" : "objects";
		this.fs_root = (this.fs && this.fs.default_root) || "apps";
		this.render_banner();
		this.render_explorer_header();
		this.render_explorer();
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
		if (this.explorer_mode === "fs") {
			this.$banner
				.html(
					`<span class="fvs-banner-tag">${__("Mode A · developer_mode")}</span> ` +
						__(
							"Filesystem IDE — edits write directly to real files under the bench. Changes take effect immediately."
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

	/* --------------------- Explorer header / dispatch ------------------- */

	render_explorer_header() {
		// Files | Objects switch — only when developer_mode makes Mode A available.
		if (this.developer_mode) {
			this.$explorer_tabs.css("display", "").empty();
			[
				["fs", __("Files")],
				["objects", __("Objects")],
			].forEach(([mode, label]) => {
				const $t = $(
					`<button class="fvs-etab ${this.explorer_mode === mode ? "active" : ""}">${label}</button>`
				);
				$t.on("click", () => this.switch_explorer(mode));
				this.$explorer_tabs.append($t);
			});
		}
		this.render_fs_roots();
	}

	render_fs_roots() {
		if (this.explorer_mode !== "fs") {
			this.$fs_roots.css("display", "none");
			return;
		}
		this.$fs_roots.css("display", "").empty();
		[
			["apps", __("apps")],
			["", __("bench")],
		].forEach(([root, label]) => {
			const $b = $(
				`<button class="fvs-root-btn ${this.fs_root === root ? "active" : ""}">${label}</button>`
			);
			$b.on("click", () => {
				this.fs_root = root;
				this.render_fs_roots();
				this.render_explorer();
			});
			this.$fs_roots.append($b);
		});
	}

	switch_explorer(mode) {
		if (this.explorer_mode === mode) return;
		this.explorer_mode = mode;
		this.render_banner();
		this.render_explorer_header();
		this.render_explorer();
	}

	render_explorer() {
		if (this.explorer_mode === "fs") {
			this.render_fs_tree();
		} else {
			this.render_tree();
		}
	}

	reload_explorer() {
		if (this.explorer_mode === "fs") {
			this.render_fs_tree();
			frappe.show_alert({ message: __("Files reloaded"), indicator: "blue" });
		} else {
			this.reload_tree();
		}
	}

	on_new_click() {
		if (this.explorer_mode === "fs") {
			this.fs_new_menu();
		} else {
			this.open_new();
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

	/* --------------------- Filesystem tree (Mode A) --------------------- */

	render_fs_tree() {
		this.$tree.empty();
		const $body = $('<div class="fvs-fs-rootbody"></div>').appendTo(this.$tree);
		this.fs_load_into($body, this.fs_root, 0);
	}

	fs_load_into($container, path, depth) {
		$container.html(`<div class="fvs-loading">${__("Loading…")}</div>`);
		frappe
			.xcall("frappe_vs.api.fs_list_dir", { path })
			.then((res) => {
				$container.empty();
				if (!res.entries.length) {
					$container.append(`<div class="fvs-tree-empty">${__("Empty folder")}</div>`);
					return;
				}
				res.entries.forEach((item) => this.render_fs_row($container, item, depth));
				this.refresh_tree_active();
			})
			.catch(() => {
				$container.html(`<div class="fvs-tree-empty">${__("Cannot read folder")}</div>`);
			});
	}

	render_fs_row($container, item, depth) {
		const pad = 8 + depth * 12;
		const esc = frappe.utils.escape_html;
		if (item.type === "dir") {
			const $row = $(`
				<div class="fvs-fs-row fvs-fs-dir" style="padding-left:${pad}px">
					<span class="fvs-chevron">▸</span>
					<span class="fvs-fs-icon">📁</span>
					<span class="fvs-fs-name">${esc(item.name)}</span>
				</div>`);
			const $body = $('<div class="fvs-fs-children" style="display:none"></div>');
			let loaded = false;
			$row.on("click", () => {
				const open = $body.is(":visible");
				$body.toggle(!open);
				$row.find(".fvs-chevron").text(open ? "▸" : "▾");
				if (!open && !loaded) {
					loaded = true;
					this.fs_load_into($body, item.path, depth + 1);
				}
			});
			$row.on("contextmenu", (e) => this.fs_context_menu(e, item));
			$container.append($row).append($body);
		} else {
			const icon = item.editable ? "📄" : item.secret ? "🔒" : "▦";
			const $row = $(`
				<div class="fvs-fs-row fvs-file ${item.editable ? "" : "fvs-fs-disabled"}"
						data-key="fs::${esc(item.path)}" style="padding-left:${pad + 14}px"
						title="${esc(item.path)}">
					<span class="fvs-file-dot"></span>
					<span class="fvs-fs-icon">${icon}</span>
					<span class="fvs-file-name">${esc(item.name)}</span>
				</div>`);
			$row.on("click", () => {
				if (item.editable) {
					this.open_fs(item.path);
				} else {
					frappe.show_alert({
						message: item.secret
							? __("{0} is protected.", [item.name])
							: __("{0} is not an editable file type.", [item.name]),
						indicator: "orange",
					});
				}
			});
			$row.on("contextmenu", (e) => this.fs_context_menu(e, item));
			$container.append($row);
		}
	}

	async open_fs(path) {
		const key = "fs::" + path;
		if (this.files.has(key)) {
			this.activate(key);
			return;
		}
		let data;
		try {
			data = await frappe.xcall("frappe_vs.api.fs_read_file", { path });
		} catch (e) {
			return;
		}
		await fvs_load_monaco();
		const model = this.monaco.editor.createModel(data.content, data.language);
		const parent = data.path.split("/").slice(0, -1).join("/");
		const file = {
			key,
			kind: "fs",
			path: data.path,
			name: data.name,
			label: data.name,
			sublabel: parent || ".",
			language: data.language,
			can_write: data.writable,
			baseline: data.content,
			dirty: false,
			model,
		};
		model.onDidChangeContent(() => this.on_model_change(key));
		this.files.set(key, file);
		this.activate(key);
	}

	fs_context_menu(e, item) {
		e.preventDefault();
		this.close_context_menu();
		const actions = [];
		if (item.type === "dir") {
			actions.push([__("New File…"), () => this.fs_create_dialog(item.path, "file")]);
			actions.push([__("New Folder…"), () => this.fs_create_dialog(item.path, "folder")]);
		}
		actions.push([__("Rename…"), () => this.fs_rename_prompt(item)]);
		actions.push([__("Delete"), () => this.fs_delete_prompt(item)]);

		const $m = $('<div class="fvs-context-menu"></div>');
		actions.forEach(([label, fn]) => {
			$('<div class="fvs-context-item"></div>')
				.text(label)
				.on("click", () => {
					this.close_context_menu();
					fn();
				})
				.appendTo($m);
		});
		$m.css({ top: e.clientY + "px", left: e.clientX + "px" });
		$("body").append($m);
		this._ctxmenu = $m;
		setTimeout(() => $(document).one("click.fvsctx", () => this.close_context_menu()), 0);
	}

	close_context_menu() {
		if (this._ctxmenu) {
			this._ctxmenu.remove();
			this._ctxmenu = null;
		}
	}

	fs_create_dialog(parent, fixedKind) {
		const fields = [];
		if (!fixedKind) {
			fields.push({
				fieldname: "kind",
				label: __("Type"),
				fieldtype: "Select",
				options: "File\nFolder",
				default: "File",
				reqd: 1,
			});
		}
		fields.push({
			fieldname: "name",
			label: __("Name"),
			fieldtype: "Data",
			reqd: 1,
			description: parent ? __("Inside {0}", [parent]) : __("At the root"),
		});
		const d = new frappe.ui.Dialog({
			title: fixedKind === "folder" ? __("New Folder") : __("New"),
			fields,
			primary_action_label: __("Create"),
			primary_action: (v) => {
				d.hide();
				const kind = fixedKind || (v.kind === "Folder" ? "folder" : "file");
				const path = (parent ? parent + "/" : "") + v.name;
				this.do_fs_create(path, kind);
			},
		});
		d.show();
	}

	do_fs_create(path, kind) {
		const method =
			kind === "folder" ? "frappe_vs.api.fs_create_folder" : "frappe_vs.api.fs_create_file";
		const args = kind === "folder" ? { path } : { path, content: "" };
		frappe.call({ method, args, freeze: true }).then((r) => {
			if (!r || !r.message) return;
			frappe.show_alert({ message: __("Created {0}", [r.message.path]), indicator: "green" });
			this.render_fs_tree();
			if (kind !== "folder") this.open_fs(r.message.path);
		});
	}

	fs_rename_prompt(item) {
		frappe.prompt(
			{ fieldname: "name", label: __("New name"), fieldtype: "Data", reqd: 1, default: item.name },
			(v) => {
				const parent = item.path.split("/").slice(0, -1).join("/");
				const new_path = (parent ? parent + "/" : "") + v.name;
				frappe
					.call({ method: "frappe_vs.api.fs_rename", args: { path: item.path, new_path }, freeze: true })
					.then((r) => {
						if (!r || !r.message) return;
						const key = "fs::" + item.path;
						if (this.files.has(key)) this.close_file(key, true);
						frappe.show_alert({ message: __("Renamed to {0}", [r.message.path]), indicator: "green" });
						this.render_fs_tree();
					});
			},
			__("Rename"),
			__("Rename")
		);
	}

	fs_delete_prompt(item) {
		frappe.confirm(__("Delete <b>{0}</b>? This cannot be undone.", [item.name]), () => {
			frappe
				.call({ method: "frappe_vs.api.fs_delete", args: { path: item.path }, freeze: true })
				.then((r) => {
					if (!r || !r.message) return;
					const key = "fs::" + item.path;
					if (this.files.has(key)) this.close_file(key, true);
					frappe.show_alert({ message: __("Deleted {0}", [item.name]), indicator: "orange" });
					this.render_fs_tree();
				});
		});
	}

	fs_new_menu() {
		this.fs_create_dialog(this.fs_root, null);
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
			kind: "object",
			doctype,
			name,
			sublabel: doctype,
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

	close_file(key, force) {
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
		if (file.dirty && !force) {
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
					<span class="fvs-tab-doctype">${frappe.utils.escape_html(file.sublabel || "")}</span>
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
		const call =
			file.kind === "fs"
				? { method: "frappe_vs.api.fs_write_file", args: { path: file.path, content: code } }
				: {
						method: "frappe_vs.api.save_source",
						args: {
							object_type: file.doctype,
							name: file.name,
							code: code,
							modified: file.modified,
						},
				  };
		frappe
			.call({
				...call,
				freeze: true,
				freeze_message: __("Saving {0}…", [file.label]),
			})
			.then((r) => {
				if (!r || !r.message) return; // server error already shown
				file.baseline = code;
				if (file.kind !== "fs") {
					file.modified = r.message.modified;
					file.label = r.message.label || file.label;
				}
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
		const flags = [(file.language || "text").toUpperCase()];
		if (!file.can_write) flags.push(__("read-only"));
		if (file.dirty) flags.push(__("unsaved"));
		const title = file.kind === "fs" ? file.path : `${file.doctype} › ${file.name}`;
		this.set_status_left(title);
		this.$status_right.text(`Ln ${ln}, Col ${col}    ${flags.join("  ·  ")}`);
	}
};
