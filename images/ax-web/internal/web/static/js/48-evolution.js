/*
 * Oficina de agentes · Evolución (#/evolucion): proposals, leaderboards by
 * agent version and by model, and the evaluation bench.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui, L } = O;
  const S = O.state;

  const rate = (a, b) => (b ? a / b : NaN);

  /** sortableTable(rows, columns, state) — click a header to sort. */
  function sortableTable(rows, cols, st) {
    const col = cols.find((c) => c.key === st.key) || cols[0];
    const sorted = rows.slice().sort((a, b) => {
      const x = col.value(a), y = col.value(b);
      const xv = Number.isFinite(x) || typeof x === "string" ? x : -Infinity;
      const yv = Number.isFinite(y) || typeof y === "string" ? y : -Infinity;
      const r = xv < yv ? -1 : xv > yv ? 1 : 0;
      return st.desc ? -r : r;
    });
    const table = h("table", { class: "table table-sort" },
      h("thead", null, h("tr", null, cols.map((c) => h("th", {
        scope: "col", class: c.num ? "num" : null,
        "aria-sort": c.key === col.key ? (st.desc ? "descending" : "ascending") : "none",
      }, h("button", {
        type: "button", class: "th-btn", title: c.title || null,
        on: {
          click: () => {
            if (st.key === c.key) st.desc = !st.desc;
            else { st.key = c.key; st.desc = c.num !== false; }
            st.redraw();
          },
        },
      }, c.label, c.key === col.key ? (st.desc ? " ↓" : " ↑") : ""))))),
      h("tbody", null, sorted.map((r) => h("tr", null, cols.map((c) => h("td", { class: c.num ? "num" : null }, c.render ? c.render(r) : c.fmt(c.value(r))))))));
    return h("div", { class: "table-wrap" }, table);
  }

  const COMMON = [
    { key: "jobs", label: "Trabajos", num: true, value: (m) => m.jobs, fmt: fmt.num },
    { key: "ok", label: "Éxito", num: true, value: (m) => rate(m.succeeded, m.jobs), fmt: fmt.pct, title: "Terminados bien sobre el total" },
    { key: "thumbs", label: "👍 / 👎", num: true, value: (m) => m.thumbs_up - m.thumbs_down, render: (m) => m.thumbs_up + " / " + m.thumbs_down },
    { key: "approved", label: "Revisiones OK", num: true, value: (m) => rate(m.approved, m.approved + m.rejected), fmt: fmt.pct, title: "Revisiones de sus cambios que los aprobaron" },
    { key: "eval", label: "Nota eval.", num: true, value: (m) => (m.eval_runs ? m.eval_score_avg : NaN), render: (m) => (m.eval_runs ? fmt.num1(m.eval_score_avg) + " (" + m.eval_runs + ")" : "—"), title: "Nota media del juez en el banco de evaluación" },
    { key: "cost", label: "Coste medio", num: true, value: (m) => m.avg_cost_usd, fmt: fmt.usd },
    { key: "secs", label: "Duración media", num: true, value: (m) => m.avg_seconds, render: (m) => fmt.duration(m.avg_seconds * 1000) },
    { key: "turns", label: "Turnos medios", num: true, value: (m) => m.avg_turns, fmt: fmt.num1 },
    { key: "total", label: "Coste total", num: true, value: (m) => m.cost_usd, fmt: fmt.usd },
    { key: "last", label: "Último uso", num: true, value: (m) => u.ms(m.last_used), render: (m) => ui.time(m.last_used) },
  ];

  function agentCols() {
    return [{
      key: "agent", label: "Agente", num: false,
      value: (m) => ((O.sel.agent(m.agent_id) || {}).name || m.agent_id) + " v" + String(m.version).padStart(4, "0"),
      render: (m) => {
        const a = O.sel.agent(m.agent_id);
        return h("a", { href: "#/agentes/" + O.enc(m.agent_id), class: "inline-agent" },
          a ? ui.avatar(a, "xs") : null, " " + (a ? a.name : m.agent_id), h("span", { class: "muted", text: " v" + m.version + (a && a.version === m.version ? " (actual)" : "") }));
      },
    }].concat(COMMON);
  }
  function modelCols() {
    return [{
      key: "model", label: "Motor · modelo", num: false,
      value: (m) => (m.harness || "") + " " + (m.model || ""),
      render: (m) => h("span", { class: "mono" }, (O.own(L.harness, m.harness) || m.harness || "?") + " · " + (m.model || "predeterminado")),
    }].concat(COMMON);
  }

  // ------------------------------------------------------------- bench --

  function evalForm(onDone) {
    const form = h("form", { class: "form", novalidate: true });
    const name = ui.input({ maxlength: 60, placeholder: "p. ej. Encontrar el bug del parser" });
    const projects = O.sel.projects().filter((p) => !p.archived);
    const proj = ui.select(projects.map((p) => ({ value: p.id, label: p.name })), S.snap.settings.retro_project);
    const kind = ui.select(["pregunta", "plan", "cambio", "revision"].map((k) => ({ value: k, label: L.kinds[k].icon + " " + L.kinds[k].label })), "pregunta");
    const prompt = ui.textarea({ rows: 5, placeholder: "La tarea congelada que recibirá cada candidato." });
    const criteria = ui.textarea({ rows: 4, placeholder: "Qué tiene que tener una respuesta de 10: hechos concretos, ficheros citados, el arreglo correcto…" });
    const submit = ui.btn("Guardar evaluación", { type: "submit", kind: "primary", icon: "check" });
    O.put(form,
      h("div", { class: "grid-3" }, ui.field("Nombre", name, { field: "name" }), ui.field("Proyecto", proj, { field: "project_id" }), ui.field("Tipo", kind, { field: "kind" })),
      ui.field("Tarea", prompt, { field: "prompt", counter: ui.counter(prompt, 32768) }),
      ui.field("Criterios del juez", criteria, { field: "criteria", hint: "Grace (la revisora) puntúa de 0 a 10 contra estos criterios." }),
      h("div", { class: "form-foot" }, h("span"), h("div", { class: "row-actions" }, ui.btn("Cancelar", { kind: "ghost", onClick: () => onDone(false) }), submit)));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ui.fieldError(form, null);
      if (!name.value.trim()) return ui.fieldError(form, { field: "name", message: "Ponle un nombre." });
      if (!prompt.value.trim()) return ui.fieldError(form, { field: "prompt", message: "Escribe la tarea." });
      if (!criteria.value.trim()) return ui.fieldError(form, { field: "criteria", message: "Sin criterios el juez no puede puntuar." });
      await ui.busy(submit, async () => {
        try {
          await O.api.post("/api/evals", { name: name.value.trim(), project_id: proj.value, kind: kind.value, prompt: prompt.value, criteria: criteria.value });
          O.toast("Evaluación añadida al banco.", { kind: "ok" });
          O.store.sync();
          onDone(true);
        } catch (ex) {
          ui.fieldError(form, ex);
        }
      });
      return undefined;
    });
    return form;
  }

  function runDialog(preselect) {
    const evals = S.snap.evals;
    const agents = O.sel.agents().filter((a) => a.enabled);
    const agent = ui.select(agents.map((a) => ({ value: a.id, label: a.emoji + " " + a.name + " (v" + a.version + ", " + ui.modelLabel(a.harness, a.model, a.effort) + ")" })), S.snap.settings.default_agent);
    const checks = evals.map((ev) => {
      const c = h("input", { type: "checkbox", checked: !preselect || preselect.includes(ev.id), value: ev.id });
      return { ev, c, el: h("label", { class: "check-row" }, c, h("span", null, h("strong", { text: ev.name }), h("span", { class: "muted", text: " · " + O.sel.projectName(ev.project_id) + " · " + (O.own(L.kinds, ev.kind) || { label: ev.kind }).label })) ) };
    });
    O.dialog({
      title: "Ejecutar el banco de evaluación",
      body: [
        h("p", { class: "muted", text: "Cada evaluación crea un pequeño equipo: el candidato hace la tarea y el juez la puntúa. Las notas se suman a las métricas de la versión actual del agente." }),
        ui.field("Candidato", agent),
        h("fieldset", { class: "checks" }, h("legend", { class: "field-label", text: "Evaluaciones" }), checks.map((x) => x.el)),
      ],
      actions: [
        { label: "Cancelar", kind: "ghost" },
        {
          label: "Ejecutar", kind: "primary",
          onClick: async () => {
            const ids = checks.filter((x) => x.c.checked).map((x) => x.ev.id);
            if (!ids.length) {
              O.toast("Elige al menos una evaluación.", { kind: "warn" });
              return false;
            }
            try {
              await O.api.post("/api/evals/run", { agent_id: agent.value, eval_ids: ids });
              O.toast(ids.length + " " + u.plural(ids.length, "evaluación en cola", "evaluaciones en cola") + " para " + (O.sel.agent(agent.value) || {}).name + ".", { kind: "ok" });
              O.store.sync();
              return true;
            } catch (e) {
              O.fail(e, "No se pudo lanzar el banco");
              return false;
            }
          },
        },
      ],
    });
  }

  function benchSection(state) {
    const evals = S.snap.evals;
    const runs = S.snap.pipelines.filter((p) => p.template === "evaluacion").slice(0, 30);
    const list = evals.length ? h("div", { class: "card-list" }, evals.map((ev) => h("article", { class: "card eval-card" },
      h("header", { class: "card-head" },
        h("span", { class: "card-icon", "aria-hidden": "true", text: "🧪" }),
        h("div", { class: "card-head-text" },
          h("h3", { class: "card-title", text: ev.name }),
          h("p", { class: "card-sub muted", text: O.sel.projectName(ev.project_id) + " · " + (O.own(L.kinds, ev.kind) || { label: ev.kind }).label })),
        ui.btn("Ejecutar", { size: "sm", icon: "play", onClick: () => runDialog([ev.id]) }),
        ui.iconBtn("trash", "Borrar evaluación", async (e) => {
          const btn = e.currentTarget;
          if (!(await O.confirm({ title: "¿Borrar «" + ev.name + "»?", text: "Las notas ya obtenidas se conservan en las métricas.", confirmLabel: "Borrar", danger: true }))) return;
          await ui.busy(btn, async () => {
            await O.api.post("/api/evals/" + O.enc(ev.id) + "/delete", {});
            O.toast("Evaluación borrada.", { kind: "ok" });
            O.store.sync();
          }, "No se pudo borrar");
        })),
      h("details", { class: "eval-details" }, h("summary", null, "Tarea y criterios"),
        h("p", { class: "pre-wrap", text: ev.prompt }), h("p", { class: "pre-wrap muted", text: "Criterios: " + ev.criteria })))))
      : ui.empty({
        cls: "empty-sm", icon: "🧪", title: "El banco está vacío",
        text: "Una evaluación es una tarea congelada con criterios. Ejecutarla con distintos agentes o versiones te dice, con una nota del juez, si un cambio de modelo o de prompt los hizo mejores o peores.",
      });
    const runsTable = runs.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, ["Cuándo", "Evaluación", "Candidato", "Estado", "Nota"].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", null, runs.map((p) => {
        const cand = O.sel.agent((p.participants || {}).candidate) || null;
        const step = (p.steps || [])[0];
        const job = step && step.job_id && O.sel.job(step.job_id);
        const ver = job && job.agent ? " v" + job.agent.version : "";
        return h("tr", null,
          h("td", null, ui.time(p.created)),
          h("td", null, h("a", { href: "#/equipos/" + O.enc(p.id), text: u.trunc(p.title || p.task || p.id, 50) })),
          h("td", null, cand ? [ui.avatar(cand, "xs"), " " + cand.name + ver] : (p.participants || {}).candidate || "—"),
          h("td", null, ui.statusPill(p.status)),
          h("td", { class: "num" }, ui.scoreBadge(p.score) || "—"));
      })))) : null;
    return [
      h("div", { class: "section-actions-row" },
        ui.btn("Nueva evaluación", { size: "sm", icon: "plus", onClick: () => { state.creating = true; state.redraw(); } }),
        evals.length ? ui.btn("Ejecutar banco con…", { size: "sm", kind: "primary", icon: "play", onClick: () => runDialog(null) }) : null),
      state.creating ? h("div", { class: "card" }, evalForm(() => { state.creating = false; state.redraw(); })) : null,
      list,
      runsTable ? [h("h3", { class: "form-h", text: "Resultados recientes" }), runsTable] : null,
    ];
  }

  O.route("/evolucion", {
    title: "Evolución",
    mount(root) {
      const totals = h("div");
      const props = h("div");
      const byAgent = h("div");
      const byModel = h("div");
      const bench = h("div");
      const history = h("div");
      const st = {
        agent: { key: "jobs", desc: true, redraw: () => { byAgent._sig = null; render(); } },
        model: { key: "jobs", desc: true, redraw: () => { byModel._sig = null; render(); } },
        bench: { creating: false, redraw: () => { bench._sig = null; render(); } },
        props: new Map(),
      };
      O.put(root,
        ui.pageHead({ title: "Evolución", icon: "📈", subtitle: "Cómo mejora el equipo: qué versiones y modelos rinden mejor, qué proponen aprender y cómo puntúan en el banco de pruebas." }),
        totals,
        ui.section("Propuestas pendientes", { icon: "💡" }, props),
        ui.section("Clasificación por versión de agente", { icon: "🏆", sub: "Cada fila es una versión: cambiar modelo, esfuerzo, modo o system prompt crea otra, así puedes comparar el antes y el después." }, byAgent),
        ui.section("Clasificación por modelo", { icon: "🧠" }, byModel),
        ui.section("Banco de evaluación", { icon: "🧪" }, bench),
        ui.section("Propuestas decididas", { icon: "🗃️" }, history));
      function render() {
        const m = S.snap.metrics;
        ui.memo(totals, JSON.stringify([m.total_cost_usd, m.cost_today_usd, m.jobs_today]), () => h("div", { class: "stats stats-wide" },
          h("div", { class: "stat" }, h("span", { class: "stat-value", text: fmt.usd(m.total_cost_usd || 0) }), h("span", { class: "stat-label", text: "coste total" })),
          h("div", { class: "stat" }, h("span", { class: "stat-value", text: fmt.usd(m.cost_today_usd || 0) }), h("span", { class: "stat-label", text: "coste hoy (UTC)" })),
          h("div", { class: "stat" }, h("span", { class: "stat-value", text: fmt.num(m.jobs_today || 0) }), h("span", { class: "stat-label", text: "trabajos hoy (UTC)" })),
          h("div", { class: "stat" }, h("span", { class: "stat-value", text: fmt.num(O.sel.pendingProposals().length) }), h("span", { class: "stat-label", text: "propuestas pendientes" }))));
        const pend = O.sel.pendingProposals();
        ui.memo(props, JSON.stringify(pend.map((p) => p.id)), () => pend.length
          ? h("div", { class: "card-list" }, pend.map((p) => {
            if (!st.props.has(p.id)) st.props.set(p.id, {});
            return ui.proposalCard(p, st.props.get(p.id));
          }))
          : ui.empty({ cls: "empty-sm", icon: "💡", text: "No hay propuestas pendientes. Las lecciones llegan al terminar los trabajos; los nuevos system prompts, cuando pides mejora al Coach desde la ficha de un agente." }));
        ui.memo(byAgent, JSON.stringify([m.by_agent_version, st.agent.key, st.agent.desc]), () => m.by_agent_version.length
          ? sortableTable(m.by_agent_version, agentCols(), st.agent)
          : ui.empty({ cls: "empty-sm", icon: "🏆", text: "Aún no hay trabajos terminados. Las métricas salen solas de cada trabajo: éxito, valoraciones, revisiones aprobadas, notas del banco, coste y duración." }));
        ui.memo(byModel, JSON.stringify([m.by_model, st.model.key, st.model.desc]), () => m.by_model.length
          ? sortableTable(m.by_model, modelCols(), st.model)
          : ui.empty({ cls: "empty-sm", icon: "🧠", text: "Sin datos por modelo todavía." }));
        ui.memo(bench, JSON.stringify([S.snap.evals, S.snap.pipelines.filter((p) => p.template === "evaluacion").map((p) => [p.id, p.status, p.score]), st.bench.creating]), () => benchSection(st.bench));
        const decided = S.snap.proposals.filter((p) => p.status !== "pendiente").slice(0, 40);
        ui.memo(history, JSON.stringify(decided.map((p) => p.id + p.status)), () => decided.length
          ? h("details", { class: "card" }, h("summary", null, decided.length + " propuestas aprobadas o rechazadas"),
            h("div", { class: "card-list" }, decided.map((p) => ui.proposalCard(p))))
          : h("p", { class: "muted", text: "Aún no has decidido ninguna propuesta." }));
      }
      render();
      return { update: render };
    },
  });
})();
