/*
 * Oficina de agentes · Bandeja (#/bandeja): everything waiting for a human.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui } = O;
  const S = O.state;

  function jobHead(j, icon) {
    const a = ui.agentOf(j);
    return h("header", { class: "card-head" },
      h("span", { class: "card-icon", "aria-hidden": "true", text: icon }),
      h("div", { class: "card-head-text" },
        h("h3", { class: "card-title" }, h("a", { href: "#/trabajo/" + O.enc(j.id), text: j.title || "(sin título)" })),
        h("p", { class: "card-sub muted" }, ui.avatar(a, "xs"), " " + a.name + " · " + O.sel.projectName(j.project_id) + " · ", ui.time(j.finished || j.created))),
      ui.statusPill(j.status));
  }

  function reviewCard(j) {
    return h("article", { class: "card inbox-card" },
      jobHead(j, "🧩"),
      h("div", { class: "chips" }, ui.diffStat(j.changes), ui.verdictBadge(j.verdict),
        j.usage && j.usage.cost_usd ? h("span", { class: "chip", text: fmt.usd(j.usage.cost_usd) }) : null,
        j.changes && j.changes.patch_truncated ? h("span", { class: "chip chip-warn", text: "parche truncado" }) : null,
        j.changes && !j.changes.contents_complete ? h("span", { class: "chip chip-warn", title: "Demasiados ficheros o demasiado grandes para crear un PR", text: "sin PR posible" }) : null),
      j.summary ? h("p", { class: "inbox-summary", text: u.trunc(j.summary, 420) }) : null,
      h("div", { class: "row-actions" },
        ui.btn("Ver cambios", { href: "#/trabajo/" + O.enc(j.id) + "?tab=cambios", kind: "primary", size: "sm", icon: "eye" }),
        j.changes && j.changes.contents_complete && O.sel.githubReady(j.project_id)
          ? ui.btn("Crear PR", { size: "sm", icon: "branch", onClick: (e) => O.act.createPR(j, e.currentTarget) }) : null,
        ui.btn("Pedir revisión", { size: "sm", icon: "send", onClick: () => O.act.followup(j) }),
        h("span", { class: "spacer" }),
        ui.btn("👍", { size: "sm", kind: "ghost", aria: "Buen trabajo", onClick: (e) => O.act.rate(j, 1, "", e.currentTarget) }),
        ui.btn("👎", { size: "sm", kind: "ghost", aria: "Mal trabajo", onClick: (e) => O.act.rate(j, -1, "", e.currentTarget) }),
        ui.btn("Descartar", { size: "sm", kind: "ghost", title: "Marcar como visto sin valorar", onClick: (e) => O.act.rate(j, 0, "visto", e.currentTarget) })));
  }

  function failedCard(j) {
    return h("article", { class: "card inbox-card is-bad" },
      jobHead(j, "⛔"),
      j.message ? h("p", { class: "inbox-error", text: u.trunc(j.message, 400) }) : null,
      h("div", { class: "row-actions" },
        ui.btn("Reintentar", { kind: "primary", size: "sm", icon: "refresh", onClick: (e) => O.act.retry(j, e.currentTarget) }),
        ui.btn("Ver registro", { href: "#/trabajo/" + O.enc(j.id) + "?tab=directo", size: "sm", icon: "eye" }),
        h("span", { class: "spacer" }),
        ui.btn("Descartar", { size: "sm", kind: "ghost", title: "Marcar como visto", onClick: (e) => O.act.rate(j, 0, "visto", e.currentTarget) })));
  }

  function stalledCard(j) {
    return h("article", { class: "card inbox-card is-warn" },
      jobHead(j, "😶"),
      h("p", null, "Lleva un buen rato sin dar señales (15 min o más). Puede estar compilando algo largo… o atascado."),
      j.activity ? h("p", { class: "muted mono", text: "Último: " + j.activity }) : null,
      h("div", { class: "row-actions" },
        ui.btn("Ver en directo", { href: "#/trabajo/" + O.enc(j.id) + "?tab=directo", kind: "primary", size: "sm", icon: "live" }),
        ui.btn("Cancelar", { size: "sm", kind: "danger-ghost", icon: "stop", onClick: (e) => O.act.cancel(j, e.currentTarget) })));
  }

  function noteCard(item) {
    if (item.type === "cleanup") {
      return h("article", { class: "card inbox-card is-bad" },
        h("header", { class: "card-head" }, h("span", { class: "card-icon", "aria-hidden": "true", text: "🧹" }),
          h("div", { class: "card-head-text" }, h("h3", { class: "card-title", text: "No se pudo borrar la tarea " + item.name }),
            h("p", { class: "card-sub muted", text: "La Oficina lo reintenta sola cada 30 s; mientras, la cola espera." }))),
        h("div", { class: "row-actions" }, ui.btn("Abrir AX", { href: "#/ax", size: "sm", icon: "server" })));
    }
    return h("article", { class: "card inbox-card is-warn" },
      h("header", { class: "card-head" }, h("span", { class: "card-icon", "aria-hidden": "true", text: "⚠️" }),
        h("div", { class: "card-head-text" }, h("h3", { class: "card-title", text: "Aviso" }), h("p", { class: "card-sub", text: item.text }))));
  }

  O.route("/bandeja", {
    title: "Bandeja",
    mount(root) {
      const body = h("div");
      const sub = h("span");
      O.put(root, ui.pageHead({ title: "Bandeja", icon: "📥", subtitle: sub }), body);
      const proposalState = new Map();
      const render = () => {
        const items = O.sel.inbox();
        const sig = JSON.stringify([items.map((i) => i.key), S.snap.proposals.map((p) => p.id + p.status),
          items.filter((i) => i.j).map((i) => [i.j.status, i.j.rating, i.j.pr, i.j.activity])]);
        if (body._sig === sig) return;
        body._sig = sig;
        const n = items.filter((i) => i.type !== "note").length;
        sub.textContent = n ? n + " " + u.plural(n, "cosa necesita", "cosas necesitan") + " tu decisión." : "Todo al día.";
        if (!items.length) {
          O.put(body, ui.empty({
            icon: "🎉", title: "Nada pendiente",
            text: [
              "Aquí aparece lo que necesita tu decisión:",
              h("ul", { class: "empty-list" },
                h("li", null, h("strong", { text: "Lecciones" }), " que los agentes proponen para la memoria de un proyecto (se inyectan en los siguientes encargos)."),
                h("li", null, h("strong", { text: "Nuevos system prompts" }), " que propone el Coach para mejorar a un agente (cada aprobación crea una versión)."),
                h("li", null, h("strong", { text: "Cambios" }), " terminados para revisar, valorar o convertir en PR."),
                h("li", null, h("strong", { text: "Fallos y atascos" }), " para reintentar o cancelar.")),
            ],
            actions: ui.btn("Encargar algo", { href: "#/nuevo", kind: "primary", icon: "plus" }),
          }));
          return;
        }
        const by = (t) => items.filter((i) => i.type === t);
        const sections = [];
        const props = by("proposal");
        if (props.length) {
          sections.push(ui.section("Propuestas de mejora", { icon: "💡", count: props.length, sub: "Los agentes aprenden de cada trabajo; tú decides qué se queda." },
            h("div", { class: "card-list" }, props.map((i) => {
              if (!proposalState.has(i.p.id)) proposalState.set(i.p.id, {});
              return ui.proposalCard(i.p, proposalState.get(i.p.id));
            }))));
        }
        const reviews = by("review");
        if (reviews.length) {
          sections.push(ui.section("Cambios para revisar", { icon: "🧩", count: reviews.length, sub: "Trabajos de tipo cambio terminados que aún no has valorado ni convertido en PR." },
            h("div", { class: "card-list" }, reviews.map((i) => reviewCard(i.j)))));
        }
        const stalled = by("stalled");
        if (stalled.length) sections.push(ui.section("Atascados", { icon: "😶", count: stalled.length }, h("div", { class: "card-list" }, stalled.map((i) => stalledCard(i.j)))));
        const failed = by("failed");
        if (failed.length) {
          sections.push(ui.section("Fallidos", { icon: "⛔", count: failed.length, sub: "De la última semana, sin reintentar ni descartar." },
            h("div", { class: "card-list" }, failed.map((i) => failedCard(i.j)))));
        }
        const notes = items.filter((i) => i.type === "cleanup" || i.type === "note");
        if (notes.length) sections.push(ui.section("Avisos del sistema", { icon: "⚠️" }, h("div", { class: "card-list" }, notes.map(noteCard))));
        O.put(body, sections);
      };
      render();
      return { update: render };
    },
  });
})();
