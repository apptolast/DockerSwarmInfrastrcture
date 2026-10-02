/*
 * Oficina de agentes · Equipos (#/equipos, #/equipos/<id>): pipelines as
 * step graphs with agent avatars, statuses and verdicts.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui, L } = O;
  const S = O.state;

  const OUTCOME = {
    aprobado: { icon: "✅", cls: "badge-ok" },
    "cambios pendientes": { icon: "🔁", cls: "badge-warn" },
    "sin veredicto": { icon: "❔", cls: "badge-soft" },
  };

  function outcomeBadge(p) {
    if (p.score !== undefined && p.score !== null) return ui.scoreBadge(p.score);
    if (!p.outcome) return null;
    const o = OUTCOME[p.outcome] || { icon: "•", cls: "badge-soft" };
    return h("span", { class: ["badge", o.cls] }, o.icon + " " + p.outcome);
  }

  function stepNode(p, st, i) {
    const job = st.job_id ? O.sel.job(st.job_id) : null;
    const agent = O.sel.agent(st.agent_id) || (job ? ui.agentOf(job) : { emoji: "❔", name: st.agent_id });
    const status = (job && job.status) || st.status || "pendiente";
    const k = L.kinds[st.kind] || { icon: "•", label: st.kind };
    const inner = [
      h("span", { class: "step-num", text: String(i + 1) }),
      ui.avatar(agent, "md", O.isRunning(status) ? "is-live" : null),
      h("span", { class: "step-name", text: st.name }),
      h("span", { class: "step-who muted", text: agent.name + " · " + k.icon + " " + k.label }),
      h("span", { class: "step-badges" },
        ui.statusPill(status),
        job ? ui.verdictBadge(job.verdict) : null,
        job ? ui.scoreBadge(job.score) : null,
        job && job.changes && job.changes.files ? ui.diffStat(job.changes) : null),
      job && O.isRunning(job.status) && job.activity ? h("span", { class: "step-activity", text: u.trunc(job.activity, 80) }) : null,
    ];
    return h("li", { class: ["step", "st-" + status] },
      st.job_id ? h("a", { class: "step-box", href: "#/trabajo/" + O.enc(st.job_id), "aria-label": "Paso " + (i + 1) + ": " + st.name + ", " + (L.status[status] || status) }, inner)
        : h("div", { class: "step-box is-pending" }, inner));
  }

  function graph(p) {
    const tpl = O.sel.template(p.template);
    const steps = p.steps || [];
    const nodes = [];
    steps.forEach((st, i) => {
      if (i > 0) nodes.push(h("li", { class: "step-arrow", "aria-hidden": "true" }, "→"));
      nodes.push(stepNode(p, st, i));
    });
    if (p.status === "en_curso" && tpl && tpl.iterative) {
      nodes.push(h("li", { class: "step-arrow", "aria-hidden": "true" }, "⋯"));
      nodes.push(h("li", { class: "step step-future" }, h("div", { class: "step-box is-pending" },
        h("span", { class: "step-name", text: "Más rondas si el revisor pide cambios" }),
        h("span", { class: "muted", text: "máximo " + p.max_iterations }))));
    }
    return h("ol", { class: "steps", "aria-label": "Pasos del equipo" }, nodes);
  }

  function pipelineCard(p, detailed) {
    const tpl = O.sel.template(p.template);
    const project = O.sel.project(p.project_id);
    const people = Object.entries(p.participants || {}).map(([role, id]) => {
      const a = O.sel.agent(id);
      const label = tpl ? (tpl.roles.find((r) => r.role === role) || {}).label || role : role;
      return a ? h("span", { class: "seat", title: label + ": " + a.name }, ui.avatar(a, "sm"), h("span", { class: "seat-label", text: label })) : null;
    });
    const cost = (p.steps || []).reduce((n, st) => {
      const j = st.job_id && O.sel.job(st.job_id);
      return n + ((j && j.usage && j.usage.cost_usd) || 0);
    }, 0);
    return h("article", { class: ["card", "pipeline", "st-" + p.status] },
      h("header", { class: "card-head" },
        h("span", { class: "card-icon", "aria-hidden": "true", text: (tpl && tpl.icon) || "🧑‍🤝‍🧑" }),
        h("div", { class: "card-head-text" },
          h("h3", { class: "card-title" }, detailed ? (p.title || (tpl && tpl.name) || p.template) : h("a", { href: "#/equipos/" + O.enc(p.id), text: p.title || (tpl && tpl.name) || p.template })),
          h("p", { class: "card-sub muted" },
            (tpl ? tpl.name : p.template) + " · " + (project ? project.name : p.project_id) + " · ",
            ui.time(p.created),
            tpl && tpl.iterative ? " · ronda " + Math.max(1, p.iteration || 1) + " de " + (p.max_iterations || 1) : "",
            cost ? " · " + fmt.usd(cost) : "")),
        h("div", { class: "card-head-side" }, ui.statusPill(p.status), outcomeBadge(p))),
      h("div", { class: "seats-row" }, people),
      graph(p),
      p.status === "en_curso" ? h("div", { class: "row-actions" },
        h("span", { class: "spacer" }),
        ui.btn("Cancelar equipo", {
          size: "sm", kind: "danger-ghost", icon: "stop",
          onClick: async (e) => {
            const btn = e.currentTarget;
            const ok = await O.confirm({ title: "¿Cancelar el equipo?", text: "Se cancela el paso en curso y no se lanzan los siguientes.", confirmLabel: "Cancelar equipo", danger: true });
            if (!ok) return;
            await ui.busy(btn, async () => {
              await O.api.post("/api/pipelines/" + O.enc(p.id) + "/cancel", {});
              O.toast("Cancelando el equipo…", { kind: "ok" });
              O.store.sync();
            }, "No se pudo cancelar");
          },
        })) : null);
  }

  function sig(list) {
    return JSON.stringify(list.map((p) => [p.id, p.status, p.outcome, p.iteration, p.steps.map((st) => {
      const j = st.job_id && O.sel.job(st.job_id);
      return [st.status, st.job_id, j && [j.status, j.verdict, j.activity, j.score]];
    })]));
  }

  O.route("/equipos", {
    title: "Equipos",
    mount(root) {
      const body = h("div");
      O.put(root, ui.pageHead({
        title: "Equipos", icon: "🧑‍🤝‍🧑",
        subtitle: "Varios agentes encadenados: cada paso es un trabajo que recibe el resultado (y los cambios) del anterior.",
        actions: ui.btn("Nuevo equipo", { href: "#/nuevo?tab=equipo", kind: "primary", icon: "plus" }),
      }), body);
      const render = () => {
        const all = S.snap.pipelines.filter((p) => p.template !== "evaluacion");
        const running = all.filter((p) => p.status === "en_curso");
        const past = all.filter((p) => p.status !== "en_curso");
        ui.memo(body, sig(all), () => {
          if (!all.length) {
            return ui.empty({
              icon: "🧑‍🤝‍🧑", title: "Aún no has reunido ningún equipo",
              text: [
                "Un equipo es una plantilla de trabajo en grupo. Por ejemplo, «plan → código → revisión»: Ada planifica, Linus implementa y Grace revisa; si Grace pide cambios, Linus corrige y Grace vuelve a revisar, hasta el máximo de rondas.",
                "También hay paneles de revisión (corrección, seguridad y tests en paralelo con una síntesis), debates de diseño y ejercicios rojo/azul de seguridad.",
              ],
              actions: ui.btn("Reunir un equipo", { href: "#/nuevo?tab=equipo", kind: "primary", icon: "team" }),
            });
          }
          return [
            running.length ? ui.section("En marcha", { icon: "⚡", count: running.length }, h("div", { class: "card-list" }, running.map((p) => pipelineCard(p)))) : null,
            past.length ? ui.section("Anteriores", { icon: "🗂️", count: past.length }, h("div", { class: "card-list" }, past.map((p) => pipelineCard(p)))) : null,
          ];
        });
      };
      render();
      return { update: render };
    },
  });

  O.route("/equipos/:id", {
    title: "Equipo",
    mount(root, params) {
      const id = params.id;
      let fetched = null;
      const body = h("div");
      O.put(root, body);
      const get = () => O.sel.pipeline(id) || fetched;
      const render = () => {
        const p = get();
        if (!p) {
          O.put(body, ui.loading("Cargando el equipo…"));
          return;
        }
        if (!Array.isArray(p.steps)) p.steps = [];
        p.participants = p.participants || {};
        const tpl = O.sel.template(p.template);
        O.router.setTitle(p.title || "Equipo");
        ui.memo(body, sig([p]) + p.task, () => [
          ui.pageHead({
            back: { href: "#/equipos", label: "Equipos" },
            title: p.title || (tpl && tpl.name) || "Equipo", icon: (tpl && tpl.icon) || "🧑‍🤝‍🧑",
            subtitle: tpl ? tpl.description : "",
          }),
          pipelineCard(p, true),
          p.task ? ui.section("Encargo", null, h("div", { class: "card" }, O.md.render(p.task))) : null,
          ui.section("Resultados de cada paso", null, h("div", { class: "card-list" }, p.steps.map((st, i) => {
            const j = st.job_id && O.sel.job(st.job_id);
            return h("article", { class: "card step-card" },
              h("header", { class: "card-head" },
                h("span", { class: "step-num", text: String(i + 1) }),
                h("div", { class: "card-head-text" },
                  h("h3", { class: "card-title" }, j ? h("a", { href: "#/trabajo/" + O.enc(j.id), text: st.name }) : st.name),
                  h("p", { class: "card-sub muted", text: (O.sel.agent(st.agent_id) || { name: st.agent_id }).name + " · " + (L.kinds[st.kind] || { label: st.kind }).label })),
                ui.statusPill((j && j.status) || st.status || "pendiente"), j ? ui.verdictBadge(j.verdict) : null),
              j && j.summary ? O.md.render(j.summary, "md-compact") : h("p", { class: "muted", text: j ? (O.isFinished(j.status) ? "Sin resumen." : "En marcha…") : "Aún no ha empezado." }));
          }))),
        ]);
      };
      if (!O.sel.pipeline(id)) {
        O.api.get("/api/pipelines/" + O.enc(id)).then((p) => {
          fetched = p.pipeline || p;
          render();
        }).catch((e) => {
          O.put(body, ui.empty({ icon: "🔎", title: "Este equipo no existe", text: e.message, actions: ui.btn("Ver equipos", { href: "#/equipos", kind: "primary" }) }));
        });
      }
      render();
      return { update: render };
    },
  });
})();
