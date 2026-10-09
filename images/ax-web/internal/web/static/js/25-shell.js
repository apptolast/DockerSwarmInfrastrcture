/*
 * Oficina de agentes · app shell: header (AX readiness, credentials,
 * queue pause, live indicator), navigation (side bar on wide screens, tab
 * bar on phones), banners, the hash router, the clock ticker and keyboard
 * shortcuts.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, fmt, ui } = O;
  const S = O.state;

  const NAV = [
    { href: "#/", label: "Oficina", icon: "office", key: "o", match: (p) => p === "/" },
    { href: "#/bandeja", label: "Bandeja", icon: "inbox", key: "b", badge: true, match: (p) => p.startsWith("/bandeja") },
    { href: "#/tablero", label: "Tablero", icon: "board", key: "t", match: (p) => p.startsWith("/tablero") || p.startsWith("/trabajo") },
    { href: "#/nuevo", label: "Nuevo", icon: "plus", key: "n", match: (p) => p.startsWith("/nuevo") },
    { href: "#/equipos", label: "Equipos", icon: "team", key: "e", match: (p) => p.startsWith("/equipos") },
    { href: "#/agentes", label: "Agentes", icon: "agent", key: "a", match: (p) => p.startsWith("/agentes") },
    { href: "#/proyectos", label: "Proyectos", icon: "folder", key: "p", match: (p) => p.startsWith("/proyectos") },
    { href: "#/turnos", label: "Turnos", icon: "clock", key: "u", match: (p) => p.startsWith("/turnos") },
    { href: "#/evolucion", label: "Evolución", icon: "trend", key: "v", match: (p) => p.startsWith("/evolucion") },
    { href: "#/ax", label: "AX", icon: "server", key: "x", match: (p) => p.startsWith("/ax") },
    { href: "#/ajustes", label: "Ajustes", icon: "sliders", key: "j", match: (p) => p.startsWith("/ajustes") },
  ];
  const TABBAR = ["#/", "#/bandeja", "#/nuevo", "#/tablero"];

  const refs = { navLinks: [], badges: [] };

  // ---------------------------------------------------------------- logo --

  function logo() {
    const s = O.s;
    return s("svg", { class: "logo", viewBox: "0 0 32 32", width: 30, height: 30, "aria-hidden": "true" },
      s("rect", { x: 1, y: 1, width: 30, height: 30, rx: 8, class: "logo-bg" }),
      s("rect", { x: 7, y: 8, width: 18, height: 12, rx: 2, class: "logo-screen" }),
      s("rect", { x: 9.5, y: 11, width: 8, height: 1.8, rx: .9, class: "logo-line" }),
      s("rect", { x: 9.5, y: 14.5, width: 11, height: 1.8, rx: .9, class: "logo-line" }),
      s("path", { d: "M13 20v3h6v-3", class: "logo-stand" }),
      s("rect", { x: 10, y: 23, width: 12, height: 2, rx: 1, class: "logo-stand-base" }),
      s("circle", { cx: 24.5, cy: 7.5, r: 3, class: "logo-led" }));
  }

  // -------------------------------------------------------------- header --

  function buildHeader() {
    const top = document.getElementById("topbar");
    refs.brand = h("span", { class: "brand-name", text: "Oficina de agentes" });
    refs.usage = h("div", { class: "usage", role: "group", "aria-label": "Uso de la suscripción", hidden: true });
    refs.axPill = h("span", { class: "ax-pill", role: "status" });
    refs.creds = h("span", { class: "creds" });
    refs.queueBtn = h("button", {
      type: "button", class: "btn btn-sm btn-ghost queue-btn",
      on: { click: toggleQueue },
    });
    const inboxBadge = h("span", { class: "nav-badge", hidden: true });
    refs.badges.push(inboxBadge);
    refs.inbox = h("a", { class: "top-inbox", href: "#/bandeja", title: "Bandeja: lo que necesita tu decisión", "aria-label": "Bandeja" },
      O.icon("inbox", { size: 19 }), inboxBadge);
    refs.live = h("span", { class: "live", role: "status", title: "Conexión en directo con la Oficina" },
      h("span", { class: "live-dot", "aria-hidden": "true" }), h("span", { class: "live-text", text: "Conectando…" }));
    O.put(top,
      h("button", { type: "button", class: "skip-link", on: { click: () => document.getElementById("view").focus() } }, "Saltar al contenido"),
      h("a", { class: "brand", href: "#/", "aria-label": "Ir a la oficina" }, logo(), refs.brand),
      h("div", { class: "top-status" }, refs.usage, refs.axPill, refs.creds, refs.queueBtn, refs.inbox, refs.live));
  }

  // One subscription window: a bar that turns amber 15 points before the gate
  // and red at it (only while the sample still counts, as on the server), with
  // the reset time in its title and in a toast on tap. A window whose reset
  // has passed is empty again, so it is not drawn.
  const USAGE_FRESH_MS = 15 * 60 * 1000;

  function usageMeter(win, bar, gate, stale, top) {
    const resets = new Date(bar.resets_at);
    if (!(resets.getTime() > O.now())) return null;
    const pct = Math.max(0, Math.min(100, Number(bar.percent) || 0));
    const stop = pct >= gate && !stale;
    const level = stop ? "is-stop" : pct >= gate - 15 ? "is-warn" : "is-ok";
    const opts = win.long ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" };
    const title = win.name + ": " + Math.round(pct) + " % usado. Se reinicia " + resets.toLocaleString("es", opts) + "." +
      (stop ? " No arranca ningún trabajo nuevo hasta entonces." : "") +
      (stale ? " Dato de hace más de 15 minutos: se renueva con el próximo trabajo." : "");
    return h("span", {
      class: ["usage-meter", level, stale ? "is-stale" : "", top ? "is-top" : ""], title, role: "button", tabindex: "0",
      "aria-label": title, on: { click: () => O.toast(title, { kind: stop ? "warn" : "ok" }) },
    },
    h("span", { class: "usage-label", text: win.short }),
    h("span", { class: "usage-track", "aria-hidden": "true" }, h("span", { class: "usage-fill", style: { "--p": pct.toFixed(1) + "%" } })),
    h("span", { class: "usage-pct", text: Math.round(pct) + " %" }));
  }

  function updateUsage(u) {
    u = u || {};
    const gate = Number(u.gate_percent) || 95;
    const sampled = u.sampled ? Date.parse(u.sampled) : NaN;
    const stale = Number.isFinite(sampled) ? O.now() - sampled > USAGE_FRESH_MS : !!u.stale;
    const wins = [
      { short: "5 h", name: "Ventana de 5 horas", long: false, bar: u.five_hour },
      { short: "Sem.", name: "Ventana semanal", long: true, bar: u.seven_day },
    ].filter((w) => w.bar);
    let top = null;
    for (const w of wins) if (!top || Number(w.bar.percent) > Number(top.bar.percent)) top = w;
    const meters = wins.map((w) => usageMeter(w, w.bar, gate, stale, w === top)).filter(Boolean);
    // The 30-second tick calls this too: leave the DOM alone when nothing changed.
    const sig = meters.map((m) => m.className + "|" + m.title).join("\n");
    if (sig === refs.usageSig) return;
    refs.usageSig = sig;
    refs.usage.hidden = meters.length === 0;
    O.put(refs.usage, ...meters);
  }

  async function toggleQueue() {
    if (!S.snap) return;
    const paused = !!S.snap.settings.queue_paused;
    await ui.busy(refs.queueBtn, async () => {
      await O.api.post("/api/queue/pause", { paused: !paused });
      S.snap.settings.queue_paused = !paused;
      O.toast(paused ? "Cola reanudada: los trabajos vuelven a arrancar." : "Cola en pausa: no arrancará ningún trabajo nuevo (el actual sigue).", { kind: "ok" });
      O.emit("store", { full: false, jobs: [] });
    }, "No se pudo cambiar la cola");
  }

  function axStatus() {
    const snap = S.snap;
    if (!snap) return { cls: "is-unknown", text: "…", title: "" };
    const ax = snap.ax || {};
    const act = O.sel.activeJob();
    if (!ax.ready) return { cls: "is-bad", text: "AX no listo", title: ax.reap_note || "El ejecutor aún no está listo (recogiendo ejecuciones anteriores o arrancando)." };
    if (ax.cleanup_failed) return { cls: "is-bad", text: "Limpieza pendiente", title: "La tarea " + ax.cleanup_failed + " no se pudo borrar; se reintenta." };
    if (act || snap.active) return { cls: "is-busy", text: "Sandbox ocupado", title: act ? (act.agent.name || "") + ": " + (act.title || "") : "Hay una ejecución en curso." };
    if (ax.blocking) return { cls: "is-busy", text: "Ocupado por el host", title: "Una tarea del host (" + ax.blocking + ") ocupa el worker; la cola espera." };
    return { cls: "is-ok", text: "Sandbox libre", title: "El único sandbox de AX está libre." };
  }

  function updateChrome() {
    const snap = S.snap;
    if (snap) {
      refs.brand.textContent = snap.settings.office_name || "Oficina de agentes";
      updateUsage(snap.usage);
      const st = axStatus();
      refs.axPill.className = "ax-pill " + st.cls;
      refs.axPill.title = st.title;
      O.put(refs.axPill, h("span", { class: "ax-lamp", "aria-hidden": "true" }), h("span", { class: "ax-text", text: st.text }));
      const c = snap.credentials || {};
      O.put(refs.creds,
        ui.creditBadge("Claude", c.claude, c.claude ? "Token de Claude Code presente" : "Falta el token de Claude Code"),
        ui.creditBadge("Codex", c.codex, c.codex ? "Credencial de Codex (" + (c.codex_source || "?") + ")" : "Codex no está configurado"),
        ui.creditBadge("GitHub", c.github, c.github ? "Token de GitHub para: " + ownersText(c.github_owners) : "Sin token de GitHub: no hay issues, PRs ni comentarios"));
      const paused = !!snap.settings.queue_paused;
      refs.queueBtn.classList.toggle("is-paused", paused);
      refs.queueBtn.setAttribute("aria-pressed", paused ? "true" : "false");
      refs.queueBtn.title = paused ? "La cola está en pausa: pulsa para reanudarla" : "Pausar la cola (el trabajo en curso sigue)";
      O.put(refs.queueBtn, O.icon(paused ? "play" : "pause", { size: 15 }), h("span", { class: "queue-text", text: paused ? "Cola en pausa" : "Pausar cola" }));
      const n = O.sel.inboxCount();
      for (const b of refs.badges) {
        b.textContent = n > 99 ? "99+" : String(n);
        b.hidden = n === 0;
      }
      refs.inbox.setAttribute("aria-label", n ? "Bandeja: " + n + " pendientes" : "Bandeja: nada pendiente");
      document.title = currentTitle + " · " + (snap.settings.office_name || "Oficina");
    }
    updateBanners();
  }

  function ownersText(list) {
    return (list || []).map((o) => (o === "*" ? "todas" : o)).join(", ") || "—";
  }
  O.ownersText = ownersText;

  function updateConn() {
    const st = S.conn;
    refs.live.className = "live is-" + st;
    refs.live.querySelector(".live-text").textContent = st === "live" ? "En directo" : st === "retrying" ? "Reconectando…" : "Conectando…";
    updateBanners();
  }

  let connLostAt = 0;
  function updateBanners() {
    const box = document.getElementById("banners");
    if (!box) return;
    const items = [];
    if (S.conn === "retrying") {
      if (!connLostAt) connLostAt = Date.now();
      if (Date.now() - connLostAt > 8000) items.push({ cls: "warn", text: "Sin conexión en directo con la Oficina; reintentando. Lo que ves puede estar desactualizado." });
    } else connLostAt = 0;
    if (S.snap && S.snap.settings.queue_paused) items.push({ cls: "info", text: "La cola está en pausa: ningún trabajo nuevo arrancará hasta que la reanudes." });
    O.put(box, items.map((b) => h("div", { class: "banner banner-" + b.cls, role: "status", text: b.text })));
  }

  // ----------------------------------------------------------------- nav --

  function navLink(item, cls) {
    const badge = item.badge ? h("span", { class: "nav-badge", hidden: true, "aria-label": "pendientes" }) : null;
    if (badge) refs.badges.push(badge);
    const a = h("a", { class: cls, href: item.href, dataset: { key: item.key } },
      O.icon(item.icon, { size: 20 }), h("span", { class: "nav-label", text: item.label }), badge);
    refs.navLinks.push({ a, item });
    return a;
  }

  function buildNav() {
    O.put(document.getElementById("sidenav"),
      h("div", { class: "nav-list" }, NAV.map((it) => navLink(it, "nav-link"))),
      h("p", { class: "nav-foot muted" }, h("kbd", { text: "?" }), " atajos de teclado"));
    const more = h("button", { type: "button", class: "tab-link", on: { click: openMore } },
      O.icon("menu", { size: 20 }), h("span", { class: "nav-label", text: "Más" }));
    refs.moreBtn = more;
    O.put(document.getElementById("tabbar"),
      NAV.filter((it) => TABBAR.includes(it.href)).map((it) => navLink(it, it.href === "#/nuevo" ? "tab-link tab-main" : "tab-link")),
      more);
  }

  function openMore() {
    const rest = NAV.filter((it) => !TABBAR.includes(it.href));
    ui.drawer.open({
      title: "Más secciones",
      body: h("nav", { class: "more-list", "aria-label": "Más secciones" },
        rest.map((it) => h("a", { class: "more-link", href: it.href, on: { click: () => ui.drawer.close() } },
          O.icon(it.icon, { size: 22 }), h("span", { text: it.label })))),
    });
  }

  function setActive(path) {
    for (const { a, item } of refs.navLinks) {
      const on = item.match(path);
      a.classList.toggle("is-active", on);
      if (on) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    const inMore = NAV.some((it) => !TABBAR.includes(it.href) && it.match(path));
    if (refs.moreBtn) refs.moreBtn.classList.toggle("is-active", inMore);
  }

  // -------------------------------------------------------------- router --

  const routes = [];
  let current = null;
  let currentTitle = "Oficina";
  let firstRender = true;

  O.route = (pattern, view) => routes.push({ parts: pattern.split("/").filter(Boolean), view });

  function parseHash() {
    const raw = (location.hash || "#/").slice(1) || "/";
    const q = raw.indexOf("?");
    const path = q >= 0 ? raw.slice(0, q) : raw;
    return { path: path || "/", query: new URLSearchParams(q >= 0 ? raw.slice(q + 1) : "") };
  }

  function match(path) {
    const parts = path.split("/").filter(Boolean).map((p) => {
      try {
        return decodeURIComponent(p);
      } catch (e) {
        return p;
      }
    });
    for (const r of routes) {
      if (r.parts.length !== parts.length) continue;
      const params = {};
      let ok = true;
      r.parts.forEach((p, i) => {
        if (p[0] === ":") params[p.slice(1)] = parts[i];
        else if (p !== parts[i]) ok = false;
      });
      if (ok) return { r, params };
    }
    return null;
  }

  function unmount() {
    if (current && current.inst && current.inst.unmount) {
      try {
        current.inst.unmount();
      } catch (e) {
        console.error(e);
      }
    }
    current = null;
    O.stream.watch("route", null);
    ui.drawer.close();
  }

  function render() {
    const { path, query } = parseHash();
    unmount();
    setActive(path);
    const root = document.getElementById("view");
    root.replaceChildren();
    if (!S.snap) {
      current = { pending: true };
      if (S.loadError) {
        O.put(root, ui.empty({
          icon: "🔌", title: "No se pudo abrir la oficina",
          text: [S.loadError.message, "Comprueba la conexión y vuelve a intentarlo."],
          actions: ui.btn("Reintentar", { kind: "primary", icon: "refresh", onClick: () => O.store.load() }),
        }));
      } else {
        O.put(root, h("div", { class: "boot" }, h("div", { class: "boot-logo", "aria-hidden": "true" }, logo()), h("p", { text: "Abriendo la oficina…" })));
      }
      return;
    }
    const m = match(path);
    const view = m ? m.r.view : notFound;
    let inst = null;
    try {
      inst = view.mount(root, m ? m.params : {}, query) || {};
    } catch (e) {
      console.error(e);
      O.put(root, ui.empty({ icon: "🐞", title: "Esta vista ha fallado", text: String(e && e.message ? e.message : e) }));
      inst = {};
    }
    current = { inst, path };
    currentTitle = inst.title || view.title || "Oficina";
    document.title = currentTitle + " · " + (S.snap.settings.office_name || "Oficina");
    if (!firstRender) {
      window.scrollTo(0, 0);
      root.focus({ preventScroll: true });
    }
    firstRender = false;
  }

  const notFound = {
    title: "No encontrado",
    mount(root) {
      O.put(root, ui.empty({
        icon: "🧭", title: "Esta página no existe",
        text: "Puede que el enlace sea antiguo.",
        actions: ui.btn("Volver a la oficina", { href: "#/", kind: "primary" }),
      }));
    },
  };

  O.router = {
    start() {
      window.addEventListener("hashchange", render);
      render();
    },
    render,
    setTitle(t) {
      currentTitle = t;
      if (S.snap) document.title = t + " · " + (S.snap.settings.office_name || "Oficina");
    },
  };
  O.go = (hash) => {
    if (location.hash === hash) render();
    else location.hash = hash;
  };

  O.on("store", (chg) => {
    if (current && current.pending) render();
    else if (current && current.inst && current.inst.update) {
      try {
        current.inst.update(chg);
      } catch (e) {
        console.error(e);
      }
    }
    updateChrome();
  });
  // A window that resets, or a sample that goes stale, while nothing else changes.
  O.on("tick", () => { if (S.snap) updateUsage(S.snap.usage); });
  O.on("conn", updateConn);

  // -------------------------------------------------------------- ticker --

  let tickN = 0;
  function tick() {
    tickN++;
    for (const el of document.querySelectorAll(".js-elapsed")) {
      el.textContent = fmt.span(Number(el.dataset.since));
    }
    if (tickN % 30 === 0) {
      for (const el of document.querySelectorAll(".js-rel")) el.textContent = fmt.rel(Number(el.dataset.t));
      O.emit("tick", tickN);
      updateBanners();
    }
    if (tickN % 10 === 0 && S.conn === "retrying") updateBanners();
  }

  // ------------------------------------------------------------ shortcuts --

  function typing(el) {
    if (!el) return false;
    const t = el.tagName;
    return t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || el.isContentEditable;
  }

  function onKey(e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) return;
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "?") {
      e.preventDefault();
      showShortcuts();
      return;
    }
    const it = NAV.find((n) => n.key === e.key);
    if (it) {
      e.preventDefault();
      O.go(it.href);
    }
  }

  function showShortcuts() {
    O.dialog({
      title: "Atajos de teclado",
      body: h("dl", { class: "shortcuts" }, NAV.map((n) => [h("dt", null, h("kbd", { text: n.key })), h("dd", { text: n.label })]),
        h("dt", null, h("kbd", { text: "?" })), h("dd", { text: "Esta ayuda" }),
        h("dt", null, h("kbd", { text: "Esc" })), h("dd", { text: "Cerrar paneles y diálogos" })),
      actions: [{ label: "Entendido", kind: "primary" }],
    });
  }

  O.shell = {
    init() {
      buildHeader();
      buildNav();
      updateConn();
      setInterval(tick, 1000);
      document.addEventListener("keydown", onKey);
    },
    logo,
  };
})();
