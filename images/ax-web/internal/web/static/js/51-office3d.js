/*
 * Oficina de agentes · Oficina 3D (#/3d): the office as a Three.js scene.
 *
 * The scene lives in its own bundle (static/3d/*.js: a trimmed Three.js and
 * 10-office3d.js), which this view fetches the first time it is opened, so the
 * rest of the panel does not pay for it. The data is the real one: each desk is
 * an agent and its state is what the office view shows (O.sel.agentState).
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, ui } = O;

  const STATE = {
    working: { text: "Trabajando", cls: "is-working" },
    attention: { text: "Pide tu atención", cls: "is-attention" },
    queued: { text: "Con trabajos en cola", cls: "is-queued" },
    idle: { text: "Libre", cls: "is-idle" },
    sleeping: { text: "Descansando", cls: "is-sleeping" },
  };
  // The server names the bundle by its hash; only that shape is ever loaded.
  const BUNDLE = /^\/assets\/office3d\.[0-9a-f]{12}\.js$/;
  const CONSULT_WINDOW_MS = 3 * 60 * 1000;

  let loading = null;
  function load3d() {
    if (window.Oficina3D) return Promise.resolve(window.Oficina3D);
    if (loading) return loading;
    const meta = document.querySelector('meta[name="oficina-3d"]');
    const src = meta ? meta.getAttribute("content") || "" : "";
    if (!BUNDLE.test(src)) return Promise.reject(new Error("Esta instalación no incluye la vista 3D."));
    loading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.async = true;
      s.onload = () => (window.Oficina3D ? resolve(window.Oficina3D) : reject(new Error("La vista 3D no se inicializó.")));
      s.onerror = () => {
        loading = null;
        s.remove();
        reject(new Error("No se pudo cargar la vista 3D. Si la Oficina se actualizó hace poco, recarga la página."));
      };
      document.head.appendChild(s);
    });
    return loading;
  }

  // What the scene draws: one desk per active agent.
  function agentsModel() {
    return O.sel.agents().filter((a) => a.enabled).map((a) => {
      const st = O.sel.agentState(a.id);
      return {
        id: a.id, name: a.name, emoji: a.emoji || "🤖", color: a.color || "#8b5cf6",
        state: st.state, title: st.job ? st.job.title || "" : "", queued: st.queued.length,
        advisor: a.advisor || "", job: st.job,
      };
    });
  }

  function summary(list) {
    const n = (s) => list.filter((a) => a.state === s).length;
    return { working: n("working"), attention: n("attention"), queued: list.reduce((t, a) => t + a.queued, 0) };
  }

  function advisorOf(list) {
    const a = list.find((x) => x.state === "working" && x.advisor);
    return a ? a.advisor : "";
  }

  O.route("/3d", {
    title: "Oficina 3D",
    mount(root) {
      let scene = null;
      let paused = false;
      let gone = false;
      let sig = "";
      // Jobs that had already finished when the view opened do not pulse. A
      // queued job is not finished: it must stay out until it ends.
      const seen = new Set(O.sel.jobs().filter((j) => O.isFinished(j.status)).map((j) => j.id));
      const reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

      const stage = h("div", { class: "o3d-stage" });
      const note = h("p", { class: "o3d-note", role: "status" }, "Cargando la escena…");
      stage.appendChild(note);
      const list = h("ul", { class: "o3d-list" });
      const stats = h("p", { class: "o3d-stats" });
      const resetBtn = ui.btn("Restablecer cámara", { kind: "ghost", icon: "refresh", onClick: () => scene && scene.resetCamera() });
      const pauseBtn = ui.btn("Pausar animación", { kind: "ghost", icon: "pause", onClick: () => {
        paused = !paused;
        if (scene) scene.setPaused(paused);
        pauseBtn.setAttribute("aria-pressed", paused ? "true" : "false");
        O.put(pauseBtn, O.icon(paused ? "play" : "pause", { size: 15 }), h("span", { text: "Pausar animación" }));
      } });
      pauseBtn.setAttribute("aria-pressed", "false");
      resetBtn.disabled = true;
      pauseBtn.disabled = true;

      O.put(root,
        ui.pageHead({
          title: "Oficina 3D", icon: "🧊",
          subtitle: "Cada escritorio es un agente de la Oficina y su estado es el real. Arrastra para girar la vista, usa la rueda o pellizca para acercar y toca un agente para ver sus trabajos.",
        }),
        h("div", { class: "o3d" },
          stage,
          h("aside", { class: "o3d-side", "aria-label": "Agentes de la oficina" },
            stats, list,
            h("div", { class: "o3d-actions" }, resetBtn, pauseBtn),
            h("p", { class: "o3d-legend" },
              h("span", { class: "o3d-dot is-working" }), "trabajando",
              h("span", { class: "o3d-dot is-queued" }), "con cola",
              h("span", { class: "o3d-dot is-attention" }), "atención",
              h("span", { class: "o3d-dot is-advisor" }), "consejero"))));

      let listSig = "";
      function render() {
        const agents = agentsModel();
        const s = summary(agents);
        // The store fires on every stream event: rebuild the list only when a
        // visible field changed.
        const sigNow = JSON.stringify(agents.map((a) => [a.id, a.name, a.emoji, a.state, a.title, a.queued]));
        if (sigNow !== listSig) {
          listSig = sigNow;
          stats.textContent = s.working + " trabajando · " + s.queued + " en cola" + (s.attention ? " · " + s.attention + " piden tu atención" : "");
          stage.setAttribute("aria-label", "Oficina en 3D: " + agents.length + " agentes, " + s.working + " trabajando, " + s.queued + " trabajos en cola");
          O.put(list, agents.map((a) => h("li", null,
            h("a", { class: "o3d-agent", href: "#/tablero?agente=" + encodeURIComponent(a.id) },
              h("span", { class: "o3d-dot " + STATE[a.state].cls, "aria-hidden": "true" }),
              h("span", { class: "o3d-agent-name", text: a.emoji + " " + a.name }),
              h("span", { class: "o3d-agent-state", text: a.state === "working" && a.title ? a.title : STATE[a.state].text })))));
        }
        if (!scene) return;
        // Finished jobs whose cost shows a second model consulted the advisor.
        const consult = [];
        for (const j of O.sel.jobs()) {
          if (!O.isFinished(j.status) || seen.has(j.id)) continue;
          seen.add(j.id);
          const fin = O.u.ms(j.finished);
          // Only a job whose agent had an advisor and whose cost lists a second model.
          if (j.agent && j.agent.advisor && j.model_cost && Object.keys(j.model_cost).length > 1 &&
              Number.isFinite(fin) && O.now() - fin < CONSULT_WINDOW_MS) consult.push(j.agent_id);
        }
        const model = {
          agents, advisorOn: !!advisorOf(agents), advisorLabel: advisorOf(agents) || (O.sel.agents().find((a) => a.advisor) || {}).advisor || "", consult,
        };
        const next = JSON.stringify([model.agents.map((a) => [a.id, a.state, a.title, a.queued, a.advisor, a.name, a.emoji, a.color]), model.advisorOn, model.advisorLabel]);
        if (next === sig && !consult.length) return;
        sig = next;
        scene.update(model);
      }

      function notice(message) {
        if (message) {
          stage.appendChild(note);
          note.textContent = message;
        } else {
          note.remove();
        }
      }

      function fallback(message) {
        if (scene) {
          scene.dispose();
          scene = null;
        }
        resetBtn.disabled = true;
        pauseBtn.disabled = true;
        stage.removeAttribute("role");
        notice(message + " La lista de la derecha sigue funcionando.");
      }

      render();
      load3d().then((api) => {
        if (gone) return;
        scene = api.create(stage, {
          onPick: (id) => { location.hash = "#/tablero?agente=" + encodeURIComponent(id); },
          onNotice: notice,
          onFallback: fallback,
        });
        if (!scene) return fallback("Este navegador no puede mostrar la escena 3D.");
        note.remove();
        stage.setAttribute("role", "img");
        resetBtn.disabled = false;
        pauseBtn.disabled = reduce;
        if (reduce) pauseBtn.title = "Tu sistema pide menos movimiento: la escena ya no se anima.";
        render();
      }).catch((e) => { if (!gone) fallback(e && e.message ? e.message : "No se pudo abrir la vista 3D."); });

      return {
        title: "Oficina 3D",
        update: render,
        unmount() {
          gone = true;
          if (scene) scene.dispose();
          scene = null;
        },
      };
    },
  });
})();
