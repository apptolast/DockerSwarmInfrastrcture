/*
 * Oficina de agentes · shared UI components: avatars, pills, cards, form
 * fields, segmented controls, tabs, the catalog-driven model picker, the
 * drawer and small helpers used by every view.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, L } = O;
  const ui = (O.ui = {});

  // ------------------------------------------------------------- agents --

  /** Merges a job's agent snapshot with the live agent (for its colour). */
  ui.agentOf = function agentOf(job) {
    const live = O.sel.agent(job.agent_id);
    const snap = job.agent || {};
    return {
      id: job.agent_id,
      name: snap.name || (live && live.name) || job.agent_id || "?",
      emoji: snap.emoji || (live && live.emoji) || "🤖",
      color: (live && live.color) || "#64748b",
      role: live ? live.role : "",
    };
  };

  /** avatar(agent, size): emoji in a circle of the agent's colour. */
  ui.avatar = function avatar(a, size, extra) {
    const ag = a || {};
    return h("span", {
      class: ["avatar", "avatar-" + (size || "md"), extra],
      style: { "--agent": u.color(ag.color) },
      title: ag.name || null,
      "aria-hidden": "true",
    }, ag.emoji || "🤖");
  };

  ui.modelLabel = function modelLabel(harness, model, effort) {
    const parts = [];
    parts.push(model || (harness === "codex" ? "codex (predet.)" : "predeterminado"));
    if (effort) parts.push(ui.effortLabel(effort).toLowerCase());
    return parts.join(" · ");
  };

  const EFFORTS = { low: "Baja", medium: "Media", high: "Alta", xhigh: "Muy alta", max: "Máxima", ultra: "Ultra" };
  ui.effortLabel = (e) => O.own(EFFORTS, e) || e || "Predeterminado";

  // -------------------------------------------------------------- pills --

  ui.statusPill = function statusPill(status, opts) {
    const o = opts || {};
    return h("span", { class: ["pill", "pill-" + status, o.cls] },
      h("span", { class: "pill-dot", "aria-hidden": "true" }),
      o.label || O.own(L.status, status) || status || "—");
  };

  ui.kindBadge = function kindBadge(kind) {
    const k = O.own(L.kinds, kind) || { label: kind, icon: "•" };
    return h("span", { class: "badge badge-kind", title: k.hint || null }, h("span", { "aria-hidden": "true", text: k.icon + " " }), k.label);
  };

  ui.verdictBadge = function verdictBadge(v) {
    if (!v) return null;
    return h("span", { class: ["badge", v === "aprobado" ? "badge-ok" : "badge-warn"] },
      v === "aprobado" ? "✅ " : "🔁 ", O.own(L.verdict, v) || v);
  };

  ui.scoreBadge = function scoreBadge(n) {
    if (n === null || n === undefined) return null;
    const cls = n >= 8 ? "badge-ok" : n >= 5 ? "badge-warn" : "badge-bad";
    return h("span", { class: ["badge", cls], title: "Nota del juez" }, "⚖️ " + n + "/10");
  };

  ui.prBadge = function prBadge(pr) {
    if (!pr || !pr.url) return null;
    return h("a", { class: "badge badge-pr", href: pr.url, target: "_blank", rel: "noopener noreferrer", title: "Pull request en GitHub" },
      O.icon("branch", { size: 13 }), " PR #" + pr.number);
  };

  ui.ratingBadge = function ratingBadge(r) {
    if (!r) return null;
    const t = r.score > 0 ? "👍" : r.score < 0 ? "👎" : "👁️";
    return h("span", { class: "badge badge-soft", title: r.note || (r.score === 0 ? "Visto" : "Valorado") }, t);
  };

  ui.diffStat = function diffStat(ch) {
    if (!ch || !(ch.files > 0)) return null;
    return h("span", { class: "diffstat", title: ch.files + " " + u.plural(ch.files, "fichero", "ficheros") },
      h("span", { class: "diff-plus", text: "+" + fmt.num(ch.additions || 0) }),
      h("span", { class: "diff-minus", text: "−" + fmt.num(ch.deletions || 0) }),
      h("span", { class: "muted", text: " · " + ch.files + " " + u.plural(ch.files, "fichero", "ficheros") }));
  };

  ui.creditBadge = function creditBadge(label, ok, title) {
    return h("span", { class: ["cred", ok ? "is-ok" : "is-missing"], title },
      h("span", { "aria-hidden": "true", text: ok ? "●" : "○" }), " " + label,
      h("span", { class: "sr-only", text: ok ? " configurado" : " sin configurar" }));
  };

  // -------------------------------------------------------------- times --

  ui.time = function time(t, opts) {
    const o = opts || {};
    const v = u.ms(t);
    if (!Number.isFinite(v)) return h("span", { class: "muted", text: o.empty || "—" });
    return h("time", {
      class: "js-rel", datetime: new Date(v).toISOString(), title: fmt.full(v),
      dataset: { t: String(v) },
    }, o.absolute ? fmt.dateTime(v) : fmt.rel(v));
  };

  /** A live duration while end is empty. */
  ui.elapsed = function elapsed(start, end) {
    const a = u.ms(start);
    if (!Number.isFinite(a)) return h("span", { class: "muted", text: "—" });
    if (Number.isFinite(u.ms(end))) return h("span", { text: fmt.span(start, end) });
    return h("span", { class: "js-elapsed", dataset: { since: String(a) } }, fmt.span(start));
  };

  /** Duration of a job: elapsed while running, total when finished. */
  ui.jobDuration = function jobDuration(j) {
    if (j.status === "en_cola") return h("span", { class: "muted" }, "en cola ", ui.time(j.created));
    if (j.usage && j.usage.duration_ms > 0 && O.isFinished(j.status) && !j.started) return h("span", { text: fmt.duration(j.usage.duration_ms) });
    return ui.elapsed(j.started || j.created, O.isFinished(j.status) ? (j.finished || j.started) : null);
  };

  // ----------------------------------------------------------- layout --

  ui.pageHead = function pageHead(o) {
    return h("header", { class: "page-head" },
      o.back ? h("a", { class: "back-link", href: o.back.href }, O.icon("back", { size: 16 }), " " + o.back.label) : null,
      h("div", { class: "page-head-row" },
        h("div", { class: "page-head-text" },
          h("h1", { class: "page-title" }, o.icon ? h("span", { class: "page-icon", "aria-hidden": "true", text: o.icon }) : null, o.title),
          o.subtitle ? h("p", { class: "page-sub" }, o.subtitle) : null),
        o.actions ? h("div", { class: "page-actions" }, o.actions) : null));
  };

  ui.section = function section(title, o, ...children) {
    const opts = o || {};
    return h("section", { class: ["section", opts.cls] },
      title || opts.actions ? h("div", { class: "section-head" },
        h("h2", { class: "section-title" }, opts.icon ? h("span", { "aria-hidden": "true", text: opts.icon + " " }) : null, title),
        opts.count !== undefined ? h("span", { class: "count", text: String(opts.count) }) : null,
        opts.actions ? h("div", { class: "section-actions" }, opts.actions) : null) : null,
      opts.sub ? h("p", { class: "section-sub muted" }, opts.sub) : null,
      children);
  };

  ui.empty = function empty(o) {
    return h("div", { class: ["empty", o.cls] },
      o.icon ? h("div", { class: "empty-icon", "aria-hidden": "true", text: o.icon }) : null,
      o.title ? h("h3", { class: "empty-title", text: o.title }) : null,
      [].concat(o.text || []).map((t) => (typeof t === "string" ? h("p", { text: t }) : t)),
      o.actions ? h("div", { class: "empty-actions" }, o.actions) : null);
  };

  ui.loading = (text) => h("div", { class: "loading", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), text || "Cargando…");

  ui.btn = function btn(label, o) {
    const opts = o || {};
    return h(opts.href ? "a" : "button", {
      type: opts.href ? null : opts.type || "button",
      href: opts.href || null,
      class: ["btn", opts.kind ? "btn-" + opts.kind : null, opts.size ? "btn-" + opts.size : null, opts.cls],
      title: opts.title || null,
      "aria-label": opts.aria || null,
      disabled: opts.disabled || null,
      download: opts.download || null,
      target: opts.target || null,
      rel: opts.target ? "noopener noreferrer" : null,
      on: opts.onClick ? { click: opts.onClick } : null,
    }, opts.icon ? O.icon(opts.icon, { size: opts.size === "sm" ? 15 : 17 }) : null, label ? h("span", { text: label }) : null);
  };

  ui.iconBtn = function iconBtn(icon, aria, onClick, cls) {
    return h("button", { type: "button", class: ["btn-icon", cls], "aria-label": aria, title: aria, on: { click: onClick } }, O.icon(icon));
  };

  /** Runs an async action with the button disabled; toasts errors. */
  ui.busy = async function busy(btn, fn, title) {
    if (btn) {
      btn.disabled = true;
      btn.classList.add("is-busy");
    }
    try {
      return await fn();
    } catch (e) {
      O.fail(e, title);
      return undefined;
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("is-busy");
      }
    }
  };

  /** Re-renders el only when sig changed. */
  ui.memo = function memo(el, sig, fn) {
    if (el._sig === sig) return false;
    el._sig = sig;
    O.put(el, fn());
    return true;
  };

  // -------------------------------------------------------------- forms --

  ui.input = (props) => h("input", Object.assign({ class: "input" }, props));
  ui.textarea = (props) => h("textarea", Object.assign({ class: "input textarea" }, props));

  ui.select = function select(options, value, props) {
    const el = h("select", Object.assign({ class: "input select" }, props || {}));
    ui.setOptions(el, options, value);
    return el;
  };
  ui.setOptions = function setOptions(el, options, value) {
    el.replaceChildren();
    for (const o of options) {
      if (o.group) {
        const g = h("optgroup", { label: o.group });
        for (const x of o.options) g.appendChild(h("option", { value: x.value, disabled: x.disabled || null, text: x.label }));
        el.appendChild(g);
      } else {
        el.appendChild(h("option", { value: o.value, disabled: o.disabled || null, text: o.label }));
      }
    }
    if (value !== undefined && value !== null) {
      el.value = String(value);
      if (el.selectedIndex < 0 && el.options.length) el.selectedIndex = 0;
    }
  };

  /** field(label, control, {hint, field, counter, cls}) */
  ui.field = function field(label, control, o) {
    const opts = o || {};
    if (!control.id) control.id = u.uid("f");
    const err = h("p", { class: "field-error", role: "alert" });
    const hintEl = opts.hint ? h("p", { class: "field-hint", id: control.id + "-hint" }, opts.hint) : null;
    if (hintEl) control.setAttribute("aria-describedby", hintEl.id);
    return h("div", { class: ["field", opts.cls], dataset: opts.field ? { field: opts.field } : null },
      label ? h("label", { class: "field-label", for: control.id }, label, opts.optional ? h("span", { class: "muted", text: " (opcional)" }) : null) : null,
      control, opts.counter || null, hintEl, err);
  };

  /** Shows a server field error next to its field, or as a toast. */
  ui.fieldError = function fieldError(root, e) {
    for (const el of root.querySelectorAll(".field.has-error")) {
      el.classList.remove("has-error");
      const p = el.querySelector(".field-error");
      if (p) p.textContent = "";
    }
    if (!e) return;
    const name = e.field || "";
    // "overrides.model" or "target.prompt" may be named by their last part.
    const last = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : "";
    const fields = Array.from(root.querySelectorAll("[data-field]"));
    const hit = fields.find((el) => el.dataset.field === name) ||
      fields.find((el) => name.startsWith(el.dataset.field + ".")) ||
      (last ? fields.find((el) => el.dataset.field === last) : null);
    if (name && hit) {
      hit.classList.add("has-error");
      const p = hit.querySelector(".field-error");
      if (p) p.textContent = e.message;
      const c = hit.querySelector("input, select, textarea");
      if (c) c.focus();
      hit.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    O.fail(e);
  };

  /** A byte counter for a textarea, red over max. */
  ui.counter = function counter(control, max, unit) {
    const el = h("span", { class: "counter", "aria-live": "polite" });
    const update = () => {
      const n = unit === "runes" ? u.runes(control.value) : u.bytes(control.value);
      el.textContent = (unit === "runes" ? fmt.num(n) + " / " + fmt.num(max) + " caracteres" : fmt.bytes(n) + " / " + fmt.bytes(max));
      el.classList.toggle("is-over", n > max);
    };
    control.addEventListener("input", update);
    update();
    return el;
  };

  /** segmented({options:[{value,label,icon,hint}], value, onChange, label}) */
  ui.segmented = function segmented(o) {
    let value = o.value;
    const hint = h("p", { class: "seg-hint field-hint" });
    const buttons = o.options.map((opt) => h("button", {
      type: "button", class: "seg-btn", dataset: { value: String(opt.value) },
      title: opt.hint || null,
      on: { click: () => api.set(opt.value, true) },
    }, opt.icon ? h("span", { "aria-hidden": "true", text: opt.icon + " " }) : null, opt.label));
    const el = h("div", { class: ["seg", o.cls] },
      h("div", { class: "seg-row", role: "group", "aria-label": o.label || null }, buttons),
      o.showHint ? hint : null);
    const api = {
      el,
      value: () => value,
      set(v, user) {
        value = v;
        for (const b of buttons) {
          const on = b.dataset.value === String(v);
          b.classList.toggle("is-on", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        }
        const cur = o.options.find((x) => String(x.value) === String(v));
        hint.textContent = cur && cur.hint ? cur.hint : "";
        if (user && o.onChange) o.onChange(v);
      },
    };
    api.set(value, false);
    return api;
  };

  /** Weekday chips (Monday first); value is an array of 0..6 (0 = Sunday). */
  ui.dayChips = function dayChips(initial) {
    const set = new Set(initial || []);
    const order = [1, 2, 3, 4, 5, 6, 0];
    const el = h("div", { class: "chips", role: "group", "aria-label": "Días de la semana" },
      order.map((d) => {
        const b = h("button", {
          type: "button", class: "chip chip-toggle", title: L.days[d],
          "aria-pressed": set.has(d) ? "true" : "false", "aria-label": L.days[d],
          on: {
            click: () => {
              if (set.has(d)) set.delete(d);
              else set.add(d);
              b.setAttribute("aria-pressed", set.has(d) ? "true" : "false");
            },
          },
        }, L.daysShort[d]);
        return b;
      }));
    return { el, value: () => Array.from(set).sort((a, b) => a - b) };
  };

  /** tabs({tabs:[{id,label,count,render}], active, onChange}) with lazy panels. */
  ui.tabs = function tabs(o) {
    const bar = h("div", { class: "tabs", role: "tablist" });
    const panels = h("div", { class: "tab-panels" });
    const map = new Map();
    for (const t of o.tabs) {
      const tid = u.uid("tab");
      const btn = h("button", {
        type: "button", role: "tab", id: tid, class: "tab", "aria-selected": "false",
        on: { click: () => api.select(t.id, true) },
      }, t.label, h("span", { class: "tab-count", text: t.count ? String(t.count) : "" }));
      const panel = h("div", { role: "tabpanel", class: "tab-panel", "aria-labelledby": tid, hidden: true });
      bar.appendChild(btn);
      panels.appendChild(panel);
      map.set(t.id, { t, btn, panel, drawn: false });
    }
    bar.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const ids = o.tabs.map((t) => t.id);
      const i = ids.indexOf(api.current);
      const next = ids[(i + (e.key === "ArrowRight" ? 1 : ids.length - 1)) % ids.length];
      api.select(next, true);
      map.get(next).btn.focus();
    });
    const api = {
      el: h("div", { class: "tabset" }, bar, panels),
      current: null,
      select(id, user) {
        if (!map.has(id)) id = o.tabs[0].id;
        api.current = id;
        for (const [k, m] of map) {
          const on = k === id;
          m.btn.setAttribute("aria-selected", on ? "true" : "false");
          m.btn.classList.toggle("is-on", on);
          m.btn.tabIndex = on ? 0 : -1;
          m.panel.hidden = !on;
          if (on && !m.drawn) {
            m.drawn = true;
            O.put(m.panel, m.t.render());
          }
        }
        if (user && o.onChange) o.onChange(id);
      },
      count(id, n) {
        const m = map.get(id);
        if (m) m.btn.querySelector(".tab-count").textContent = n ? String(n) : "";
      },
      panel: (id) => (map.get(id) || {}).panel,
    };
    api.select(o.active || o.tabs[0].id, false);
    return api;
  };

  /** A <details> based menu: items [{label, icon, href|onClick}]. */
  ui.menu = function menu(label, items, o) {
    const opts = o || {};
    const d = h("details", { class: ["menu", opts.cls] });
    const close = () => { d.open = false; };
    d.append(
      h("summary", { class: ["btn", opts.kind ? "btn-" + opts.kind : null, "btn-sm"], "aria-haspopup": "true" },
        opts.icon ? O.icon(opts.icon, { size: 15 }) : null, h("span", { text: label }), O.icon("chevronDown", { size: 14 })),
      h("div", { class: "menu-pop", role: "menu" }, items.filter(Boolean).map((it) => it.href
        ? h("a", { class: "menu-item", role: "menuitem", href: it.href, on: { click: close } }, it.icon ? h("span", { "aria-hidden": "true", text: it.icon }) : null, it.label)
        : h("button", { type: "button", class: "menu-item", role: "menuitem", on: { click: () => { close(); it.onClick(); } } },
          it.icon ? h("span", { "aria-hidden": "true", text: it.icon }) : null, it.label))));
    d.addEventListener("keydown", (e) => { if (e.key === "Escape") { close(); d.querySelector("summary").focus(); } });
    if (!menuListener) {
      // One listener closes any open menu on an outside click.
      menuListener = true;
      document.addEventListener("click", (e) => {
        for (const m of document.querySelectorAll("details.menu[open]")) if (!m.contains(e.target)) m.open = false;
      });
    }
    return d;
  };
  let menuListener = false;

  /**
   * Copies text exactly as it is; when it carries invisible or control
   * characters the toast says so instead of a plain "copied".
   */
  ui.copyText = async function copyText(text, okMessage) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      O.toast("El navegador no permitió copiar", { kind: "warn" });
      return;
    }
    const warn = O.invisibleWarning(text);
    if (warn) O.toast(warn + " Se han copiado tal cual: revísalo antes de pegarlo.", { kind: "warn", title: "Copiado con caracteres ocultos", timeout: 10000 });
    else O.toast(okMessage || "Copiado al portapapeles", { kind: "ok", timeout: 1800 });
  };

  ui.copyBtn = function copyBtn(getText, label) {
    return ui.btn(label || "Copiar", {
      icon: "copy", size: "sm", kind: "ghost",
      onClick: () => ui.copyText(String(typeof getText === "function" ? getText() : getText)),
    });
  };

  /**
   * A live warning for a text control: hidden while its value has no
   * invisible or control characters, otherwise it lists them.
   */
  ui.invisibleNote = function invisibleNote(control) {
    const el = h("p", { class: "notice notice-warn invisible-note", role: "status", hidden: true });
    const update = () => {
      const warn = O.invisibleWarning(control.value);
      el.hidden = !warn;
      el.textContent = warn ? "⚠️ " + warn + " Se muestran como ⟦U+…⟧ en la vista previa; revísalos antes de guardar." : "";
    };
    control.addEventListener("input", update);
    update();
    el.refresh = update;
    return el;
  };

  /** A collapsible block of preformatted text. */
  ui.preBlock = function preBlock(title, text, o) {
    const opts = o || {};
    return h("details", { class: "pre-block", open: opts.open || null },
      h("summary", null, h("span", { text: title }), h("span", { class: "muted", text: " · " + fmt.bytes(u.bytes(text)) })),
      h("div", { class: "pre-tools" }, ui.copyBtn(() => text)),
      h("pre", { class: "pre mono", text: text ? O.visible(text) : "(vacío)" }));
  };

  // ------------------------------------------------------- model picker --

  const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/[\]-]{0,127}$/;
  ui.MODEL_RE = MODEL_RE;

  /**
   * modelPicker({harness, model, effort, fallback_model}, {fallback, onChange})
   * Harness → model (catalog + "otro…") → effort filtered by the model.
   */
  ui.modelPicker = function modelPicker(init, o) {
    const opts = o || {};
    const cat = O.state.snap.catalog;
    const st = {
      harness: init.harness === "codex" ? "codex" : "claude",
      model: init.model || "",
      effort: init.effort || "",
      fallback: init.fallback_model || "",
    };
    const models = (hn) => (cat[hn] && cat[hn].models) || [];
    const catModel = (hn, m) => models(hn).find((x) => x.id === m) || null;
    const effortsFor = (hn, m) => {
      const cm = catModel(hn, m);
      return cm && cm.efforts && cm.efforts.length ? cm.efforts : (cat[hn] && cat[hn].efforts) || [];
    };

    const hSel = ui.select([
      { value: "claude", label: "Claude Code" + (cat.claude.version ? " " + cat.claude.version : "") },
      { value: "codex", label: "Codex" + (cat.codex.version ? " " + cat.codex.version : "") },
    ], st.harness);
    const mSel = ui.select([], "");
    const other = ui.input({ placeholder: "p. ej. claude-opus-5-5", autocomplete: "off", spellcheck: "false", maxlength: 128, class: "input mono" });
    const otherField = ui.field("Modelo (texto libre)", other, { hint: "Cualquier identificador que acepte el CLI. Se valida antes de lanzar." });
    const eSel = ui.select([], "");
    const fSel = ui.select([], "");
    const fField = ui.field("Modelo de reserva", fSel, { optional: true, hint: "Claude lo usa si el principal está saturado." });
    const srcNote = h("p", { class: "field-hint" });

    function fillModels() {
      const list = models(st.harness);
      const inCat = !st.model || !!catModel(st.harness, st.model);
      ui.setOptions(mSel, [
        { value: "", label: st.harness === "codex" ? "Predeterminado de Codex" : "Predeterminado de Claude Code" },
        { group: "Catálogo", options: list.map((m) => ({ value: m.id, label: m.label + (m.label !== m.id ? " · " + m.id : "") })) },
        { value: "__other", label: "Otro (escribir)…" },
      ], inCat ? st.model : "__other");
      other.value = inCat ? "" : st.model;
      otherField.hidden = inCat;
      const c = cat[st.harness] || {};
      srcNote.textContent = c.source ? "Catálogo: " + c.source + "." : "";
    }
    function fillEfforts() {
      const list = effortsFor(st.harness, st.model);
      const cm = catModel(st.harness, st.model);
      if (st.effort && !list.includes(st.effort)) st.effort = "";
      ui.setOptions(eSel, [{ value: "", label: "Predeterminado" + (cm && cm.default_effort ? " (" + ui.effortLabel(cm.default_effort).toLowerCase() + ")" : "") }]
        .concat(list.map((e) => ({ value: e, label: ui.effortLabel(e) + (cm && cm.default_effort === e ? " · por defecto" : "") }))), st.effort);
    }
    function fillFallback() {
      fField.hidden = st.harness !== "claude" || opts.fallback === false;
      ui.setOptions(fSel, [{ value: "", label: "Ninguno" }].concat(models("claude").map((m) => ({ value: m.id, label: m.label }))), st.fallback);
      if (fSel.value !== st.fallback) {
        fSel.appendChild(h("option", { value: st.fallback, text: st.fallback }));
        fSel.value = st.fallback;
      }
    }
    const changed = () => { if (opts.onChange) opts.onChange(api.value()); };

    hSel.addEventListener("change", () => {
      st.harness = hSel.value;
      // A model of the other CLI makes no sense: start from its default.
      const first = models(st.harness)[0];
      st.model = opts.pickFirst && first ? first.id : "";
      st.effort = opts.pickFirst && first && first.default_effort ? first.default_effort : "";
      st.fallback = "";
      fillModels(); fillEfforts(); fillFallback(); changed();
    });
    mSel.addEventListener("change", () => {
      if (mSel.value === "__other") {
        otherField.hidden = false;
        st.model = other.value.trim();
        other.focus();
      } else {
        otherField.hidden = true;
        st.model = mSel.value;
        const cm = catModel(st.harness, st.model);
        if (cm && cm.default_effort && !st.effort) st.effort = "";
      }
      fillEfforts(); changed();
    });
    other.addEventListener("input", () => { st.model = other.value.trim(); fillEfforts(); changed(); });
    eSel.addEventListener("change", () => { st.effort = eSel.value; changed(); });
    fSel.addEventListener("change", () => { st.fallback = fSel.value; changed(); });

    fillModels(); fillEfforts(); fillFallback();
    const el = h("div", { class: "model-picker" },
      h("div", { class: "grid-3" },
        ui.field("Motor (harness)", hSel, { field: "harness" }),
        ui.field("Modelo", mSel, { field: "model" }),
        ui.field("Esfuerzo de razonamiento", eSel, { field: "effort" })),
      otherField, fField, srcNote);
    const api = {
      el,
      value: () => ({ harness: st.harness, model: st.model, effort: st.effort, fallback_model: st.harness === "claude" ? st.fallback : "" }),
      validate() {
        if (st.model && !MODEL_RE.test(st.model)) return { field: "model", message: "El modelo solo admite letras, números y . _ : / [ ] - (máx. 128)." };
        if (mSel.value === "__other" && !st.model) return { field: "model", message: "Escribe el modelo o elige uno del catálogo." };
        if (st.fallback && st.fallback === st.model) return { field: "model", message: "El modelo de reserva debe ser distinto del principal." };
        return null;
      },
    };
    return api;
  };

  // ------------------------------------------------------------ job card --

  ui.jobModel = function jobModel(j) {
    const a = j.agent || {};
    return ui.modelLabel(a.harness, (j.usage && j.usage.model_used) || a.model, a.effort);
  };

  ui.jobCard = function jobCard(j, o) {
    const opts = o || {};
    const a = ui.agentOf(j);
    const k = O.own(L.kinds, j.kind) || { icon: "•", label: j.kind };
    const cost = j.usage && j.usage.cost_usd > 0 ? fmt.usd(j.usage.cost_usd) : "";
    return h("a", { class: ["jcard", "st-" + j.status, j.stalled && "is-stalled"], href: "#/trabajo/" + O.enc(j.id) },
      h("div", { class: "jcard-top" },
        ui.avatar(a, "sm"),
        h("div", { class: "jcard-head" },
          h("span", { class: "jcard-title", text: j.title || "(sin título)" }),
          h("span", { class: "jcard-sub" },
            h("span", { title: k.label, text: k.icon + " " + k.label }), " · ", O.sel.projectName(j.project_id))),
        opts.position ? h("span", { class: "jcard-pos", title: "Posición en la cola", text: "#" + opts.position }) : null),
      O.isRunning(j.status) && (j.activity || j.stalled)
        ? h("p", { class: ["jcard-activity", j.stalled && "is-stalled"], text: j.stalled ? "😶 Sin noticias desde hace un rato" : j.activity }) : null,
      j.status === "en_cola" && j.waiting ? h("p", { class: "jcard-activity is-waiting", text: "⏳ " + j.waiting }) : null,
      h("div", { class: "jcard-foot" },
        opts.status === false ? null : ui.statusPill(j.status),
        h("span", { class: "jcard-model mono", text: ui.jobModel(j) }),
        h("span", { class: "jcard-time" }, ui.jobDuration(j)),
        cost ? h("span", { class: "jcard-cost", text: cost }) : null,
        j.priority === 2 ? h("span", { class: "badge badge-hot", text: "Alta" }) : null,
        ui.verdictBadge(j.verdict), ui.scoreBadge(j.score), ui.diffStat(j.changes), ui.ratingBadge(j.rating),
        j.pr ? h("span", { class: "badge badge-pr" }, "PR #" + j.pr.number) : null));
  };

  // -------------------------------------------------------------- drawer --

  let drawerApi = null;
  /** drawer.open({title, label, body, onClose}) — one drawer at a time. */
  ui.drawer = {
    open(o) {
      ui.drawer.close();
      const root = document.getElementById("drawer-root");
      const opener = document.activeElement;
      const tid = u.uid("drw");
      const title = h("h2", { id: tid, class: "drawer-title" }, o.title || "");
      const body = h("div", { class: "drawer-body" }, o.body || null);
      const panel = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": tid, tabindex: "-1" },
        h("div", { class: "drawer-grip", "aria-hidden": "true" }),
        h("div", { class: "drawer-head" }, title,
          h("button", { type: "button", class: "btn-icon", "aria-label": "Cerrar panel", on: { click: () => ui.drawer.close() } }, O.icon("close"))),
        body);
      const backdrop = h("div", { class: "drawer-backdrop", on: { click: () => ui.drawer.close() } });
      const onKey = (e) => { if (e.key === "Escape") ui.drawer.close(); };
      document.addEventListener("keydown", onKey);
      O.put(root, backdrop, panel);
      root.classList.add("is-open");
      requestAnimationFrame(() => panel.focus());
      drawerApi = {
        body, title, panel,
        close() {
          document.removeEventListener("keydown", onKey);
          root.classList.remove("is-open");
          root.replaceChildren();
          drawerApi = null;
          if (o.onClose) o.onClose();
          if (opener && opener.focus && document.contains(opener)) opener.focus();
        },
      };
      return drawerApi;
    },
    close() {
      if (drawerApi) drawerApi.close();
    },
    current: () => drawerApi,
  };

  // ---------------------------------------------------------- validation --

  ui.valid = {
    id: (s) => /^[a-z][a-z0-9-]{1,31}$/.test(s),
    color: (s) => /^#[0-9a-fA-F]{6}$/.test(s),
    branch: (s) => s === "" || (/^[A-Za-z0-9._/-]{1,200}$/.test(s) && !s.includes("..") && !/^[-/.]/.test(s) && !/[/.]$/.test(s) && !s.includes("//") && !/\.lock$/.test(s)),
    repo(s) {
      const hosts = (O.state.snap && O.state.snap.limits.repo_hosts) || ["github.com"];
      const m = /^https:\/\/([^/]+)\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(\.git)?$/.exec(s);
      return !!m && hosts.includes(m[1].toLowerCase());
    },
    tool: (s) => /^[A-Za-z][A-Za-z0-9_*():. -]{0,80}$/.test(s),
    url: (s) => s === "" || /^https:\/\/[^\s]+$/.test(s),
  };
})();
