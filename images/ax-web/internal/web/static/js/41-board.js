/*
 * Oficina de agentes · Tablero (#/tablero): kanban of jobs.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, fmt, ui } = O;

  const COLUMNS = [
    { id: "queue", title: "En cola", icon: "🎫", hint: "Esperan su turno: el sandbox ejecuta un trabajo cada vez, por prioridad y antigüedad." },
    { id: "running", title: "En curso", icon: "⚡", hint: "Trabajando ahora en el sandbox." },
    { id: "review", title: "Para revisar", icon: "🧩", hint: "Cambios terminados esperando tu valoración o un PR." },
    { id: "done", title: "Hecho", icon: "✅", hint: "Terminados correctamente." },
    { id: "failed", title: "Fallido / cancelado", icon: "⛔", hint: "No terminaron bien. Se pueden reintentar." },
  ];

  function column(j) {
    if (j.status === "en_cola") return "queue";
    if (O.isRunning(j.status)) return "running";
    if (j.status === "hecho") return O.sel.needsReview(j) ? "review" : "done";
    return "failed";
  }

  const filters = { project: "", agent: "", kind: "", q: "" };

  O.route("/tablero", {
    title: "Tablero",
    mount(root, params, query) {
      if (query.get("proyecto")) filters.project = query.get("proyecto");
      if (query.get("agente")) filters.agent = query.get("agente");
      const limits = { done: 30, failed: 20 };
      const projSel = ui.select([{ value: "", label: "Todos los proyectos" }].concat(O.sel.projects().map((p) => ({ value: p.id, label: p.name }))), filters.project, { "aria-label": "Filtrar por proyecto" });
      const agentSel = ui.select([{ value: "", label: "Todos los agentes" }].concat(O.sel.agents().map((a) => ({ value: a.id, label: a.emoji + " " + a.name }))), filters.agent, { "aria-label": "Filtrar por agente" });
      const kindSel = ui.select([{ value: "", label: "Todos los tipos" }].concat(Object.keys(O.L.kinds).map((k) => ({ value: k, label: O.L.kinds[k].icon + " " + O.L.kinds[k].label }))), filters.kind, { "aria-label": "Filtrar por tipo" });
      const search = ui.input({ type: "search", placeholder: "Buscar por título…", value: filters.q, "aria-label": "Buscar por título" });
      const board = h("div", { class: "kanban" });
      const totals = h("span");
      const onFilter = () => {
        filters.project = projSel.value;
        filters.agent = agentSel.value;
        filters.kind = kindSel.value;
        filters.q = search.value.trim().toLowerCase();
        board._sig = null;
        render();
      };
      for (const el of [projSel, agentSel, kindSel]) el.addEventListener("change", onFilter);
      search.addEventListener("input", O.u.debounce(onFilter, 200));

      O.put(root,
        ui.pageHead({
          title: "Tablero", icon: "🗂️", subtitle: totals,
          actions: ui.btn("Nuevo trabajo", { href: "#/nuevo", kind: "primary", icon: "plus" }),
        }),
        h("div", { class: "filters" }, projSel, agentSel, kindSel, search),
        board);

      function render() {
        const jobs = O.sel.jobs().filter((j) =>
          (!filters.project || j.project_id === filters.project) &&
          (!filters.agent || j.agent_id === filters.agent) &&
          (!filters.kind || j.kind === filters.kind) &&
          (!filters.q || (j.title || "").toLowerCase().includes(filters.q)));
        const cols = { queue: [], running: [], review: [], done: [], failed: [] };
        for (const j of jobs) cols[column(j)].push(j);
        // The queue column follows the dispatcher's order.
        const order = new Map(O.sel.queueJobs().map((j, i) => [j.id, i]));
        cols.queue.sort((a, b) => (order.has(a.id) ? order.get(a.id) : 1e9) - (order.has(b.id) ? order.get(b.id) : 1e9));
        const sig = JSON.stringify([limits, jobs.map((j) => [j.id, j.status, j.activity, j.stalled, j.verdict, j.rating && j.rating.score, j.pr && j.pr.number, j.priority, j.waiting, j.usage && j.usage.cost_usd])]);
        if (board._sig === sig) return;
        board._sig = sig;
        const cost = jobs.reduce((n, j) => n + ((j.usage && j.usage.cost_usd) || 0), 0);
        totals.textContent = fmt.num(jobs.length) + " trabajos · " + fmt.usd(cost) + " en total";
        O.put(board, COLUMNS.map((c) => {
          const list = cols[c.id];
          const lim = limits[c.id];
          const shown = lim ? list.slice(0, lim) : list;
          return h("section", { class: "kcol kcol-" + c.id, "aria-label": c.title },
            h("header", { class: "kcol-head", title: c.hint },
              h("span", { "aria-hidden": "true", text: c.icon }), h("h2", { class: "kcol-title", text: c.title }),
              h("span", { class: "count", text: String(list.length) })),
            h("div", { class: "kcol-body" },
              shown.length ? shown.map((j) => ui.jobCard(j, c.id === "queue" ? { position: (order.has(j.id) ? order.get(j.id) : 0) + 1, status: false } : { status: c.id === "failed" })) : h("p", { class: "kcol-empty muted", text: c.hint }),
              lim && list.length > lim ? ui.btn("Mostrar más (" + (list.length - lim) + ")", {
                size: "sm", kind: "ghost", cls: "kcol-more",
                onClick: () => { limits[c.id] += 40; render(); },
              }) : null));
        }));
      }
      render();
      return { update: render };
    },
  });
})();
