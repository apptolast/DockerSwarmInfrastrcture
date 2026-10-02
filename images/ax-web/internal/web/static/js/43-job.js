/*
 * Oficina de agentes · Trabajo (#/trabajo/<id>): one job in detail, with
 * the live timeline (the stream watches this job while the view is open),
 * the result as safe Markdown, the diff viewer and every action.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui, L } = O;
  const S = O.state;

  function sourceText(src) {
    if (!src || !src.type || src.type === "manual") return null;
    const label = L.source[src.type] || src.type;
    if ((src.type === "issue" || src.type === "pr") && src.number) return (src.type === "pr" ? "PR #" : "Issue #") + src.number;
    if (src.job_id) return h("span", null, label + " ", h("a", { href: "#/trabajo/" + O.enc(src.job_id), class: "mono", text: src.job_id }));
    if (src.ref) return label + " · " + src.ref;
    return label;
  }

  function metaGrid(j) {
    const a = ui.agentOf(j);
    const ag = j.agent || {};
    const p = O.sel.project(j.project_id);
    const pipe = j.pipeline_id ? O.sel.pipeline(j.pipeline_id) : null;
    const usage = j.usage || {};
    const items = [
      ["Agente", h("a", { href: "#/agentes/" + O.enc(j.agent_id), class: "inline-agent" }, ui.avatar(a, "xs"), " " + a.name, h("span", { class: "muted", text: " · v" + (ag.version || "?") }))],
      ["Proyecto", p ? h("span", null, h("a", { href: "#/proyectos/" + O.enc(p.id), text: p.name }), h("span", { class: "muted mono", text: " · " + (j.branch || p.branch) })) : j.project_id],
      ["Motor", h("span", { class: "mono" }, (L.harness[ag.harness] || ag.harness || "—") + " · " + (ag.model || "predeterminado") +
        (usage.model_used && usage.model_used !== ag.model ? " → " + usage.model_used : ""))],
      ["Esfuerzo", ui.effortLabel(ag.effort)],
      ["Modo", h("span", { title: (L.modes[ag.mode] || {}).hint || null, text: (L.modes[ag.mode] || {}).label || ag.mode || "—" })],
      ["Prioridad", L.priority[j.priority] || String(j.priority)],
      ["Coste", usage.cost_usd ? fmt.usd(usage.cost_usd) : "—"],
      ["Tokens", usage.in_tokens || usage.out_tokens
        ? h("span", { title: "caché leída " + fmt.tokens(usage.cache_read_tokens) + " · escrita " + fmt.tokens(usage.cache_write_tokens) }, fmt.tokens(usage.in_tokens) + " ent. · " + fmt.tokens(usage.out_tokens) + " sal.")
        : "—"],
      ["Turnos", usage.turns ? fmt.num(usage.turns) + (ag.max_turns ? " / " + ag.max_turns : "") : "—"],
      ["Duración", j.started ? ui.elapsed(j.started, O.isFinished(j.status) ? j.finished : null) : "—"],
      ["Creado", ui.time(j.created, { absolute: true })],
      j.started ? ["Empezó", ui.time(j.started, { absolute: true })] : null,
      j.finished ? ["Terminó", ui.time(j.finished, { absolute: true })] : null,
      pipe || j.pipeline_id ? ["Equipo", h("a", { href: "#/equipos/" + O.enc(j.pipeline_id) }, pipe ? (pipe.title || pipe.template) : j.pipeline_id, j.step !== undefined && pipe ? " · paso " + (j.step + 1) + (pipe.steps[j.step] ? " (" + pipe.steps[j.step].name + ")" : "") : "")] : null,
      sourceText(j.source) ? ["Origen", sourceText(j.source)] : null,
      j.apply_from ? ["Parte de", h("a", { href: "#/trabajo/" + O.enc(j.apply_from), class: "mono", text: j.apply_from })] : null,
      j.task ? ["Tarea AX", h("code", { class: "mono", text: j.task })] : null,
      j.outcome && j.status !== "hecho" ? ["Final", j.outcome + (j.exit_code !== undefined && j.exit_code !== null ? " · código " + j.exit_code : "")] : null,
    ].filter(Boolean);
    return h("dl", { class: "meta-grid" }, items.map(([k, v]) => h("div", { class: "meta-item" }, h("dt", { text: k }), h("dd", null, v))));
  }

  function nowBanner(j) {
    if (j.status === "en_cola") {
      const pos = O.sel.queueJobs().findIndex((x) => x.id === j.id);
      return h("div", { class: "notice notice-info" },
        h("strong", { text: "🎫 En cola" + (pos >= 0 ? " · posición " + (pos + 1) : "") + ". " }),
        S.snap.settings.queue_paused ? "La cola está en pausa. " : "",
        j.waiting ? j.waiting : "Empezará cuando el sandbox quede libre.");
    }
    if (O.isRunning(j.status)) {
      return h("div", { class: ["notice", j.stalled ? "notice-warn" : "notice-live"] },
        h("span", { class: "live-dot", "aria-hidden": "true" }),
        h("strong", { text: (L.status[j.status] || j.status) + ": " }),
        j.stalled ? "sin noticias desde hace un rato (15 min o más)." : j.activity || "trabajando…");
    }
    if (j.status === "fallido" && j.message) {
      return h("div", { class: "notice notice-bad" }, h("strong", { text: "⛔ Falló: " }), j.message);
    }
    if (j.status === "cancelado") return h("div", { class: "notice" }, "⏹️ Cancelado. ", j.message || "");
    return null;
  }

  // ------------------------------------------------------------- actions --

  function actionsBar(j, detail) {
    const btns = [];
    const finished = O.isFinished(j.status);
    if (j.status === "en_cola" || O.isRunning(j.status)) {
      btns.push(ui.btn(j.status === "en_cola" ? "Quitar de la cola" : "Cancelar", { kind: "danger", icon: "stop", size: "sm", onClick: (e) => O.act.cancel(j, e.currentTarget) }));
    }
    if (j.status === "en_cola") {
      const seg = ui.segmented({
        label: "Prioridad", cls: "seg-sm",
        options: [{ value: 0, label: "Baja" }, { value: 1, label: "Normal" }, { value: 2, label: "Alta", icon: "🔥" }],
        value: j.priority, onChange: (v) => O.act.priority(j, Number(v)),
      });
      btns.push(h("span", { class: "inline-label" }, h("span", { class: "muted", text: "Prioridad " }), seg.el));
    }
    if (finished) {
      btns.push(ui.btn("Reintentar", { icon: "refresh", size: "sm", onClick: (e) => O.act.retry(j, e.currentTarget, true) }));
      btns.push(ui.btn("Pedir a…", { icon: "send", size: "sm", onClick: () => O.act.followup(j) }));
    }
    if (j.changes && j.changes.files > 0 && finished) {
      btns.push(ui.btn("Descargar parche", { href: "/api/jobs/" + O.enc(j.id) + "/patch?download=1", icon: "download", size: "sm", download: j.id + ".patch" }));
      if (j.pr) btns.push(ui.btn("Ver PR #" + j.pr.number, { href: j.pr.url, target: "_blank", icon: "external", size: "sm", kind: "primary" }));
      else if (detail && detail.can_pr) btns.push(ui.btn("Crear PR", { icon: "branch", size: "sm", kind: "primary", onClick: (e) => O.act.createPR(j, e.currentTarget) }));
    }
    if (finished && j.source && (j.source.type === "issue" || j.source.type === "pr") && j.source.number && O.sel.githubReady(j.project_id)) {
      btns.push(ui.btn("Comentar en " + (j.source.type === "pr" ? "el PR" : "la issue") + " #" + j.source.number, { icon: "send", size: "sm", onClick: (e) => O.act.comment(j, e.currentTarget) }));
    }
    if (finished) {
      btns.push(h("span", { class: "spacer" }));
      btns.push(ui.btn("Borrar", {
        icon: "trash", size: "sm", kind: "danger-ghost",
        onClick: async (e) => {
          if (await O.act.remove(j, e.currentTarget)) O.go("#/tablero");
        },
      }));
    }
    return btns.length ? h("div", { class: "action-bar" }, btns) : null;
  }

  function ratingBox(j) {
    const r = j.rating;
    const note = ui.input({ maxlength: 500, placeholder: "Nota opcional: qué estuvo bien o mal (la lee el Coach)", value: r ? r.note || "" : "" });
    let score = r ? r.score : null;
    const up = h("button", { type: "button", class: ["rate-btn", score === 1 && "is-on"], "aria-pressed": score === 1 ? "true" : "false", "aria-label": "Bien hecho", on: { click: () => pick(1) } }, "👍");
    const down = h("button", { type: "button", class: ["rate-btn", score === -1 && "is-on"], "aria-pressed": score === -1 ? "true" : "false", "aria-label": "Mal hecho", on: { click: () => pick(-1) } }, "👎");
    const save = ui.btn("Guardar valoración", { size: "sm", kind: "primary", onClick: (e) => O.act.rate(j, score === null ? 0 : score, note.value.trim(), e.currentTarget) });
    function pick(v) {
      score = score === v ? 0 : v;
      up.classList.toggle("is-on", score === 1);
      down.classList.toggle("is-on", score === -1);
      up.setAttribute("aria-pressed", score === 1 ? "true" : "false");
      down.setAttribute("aria-pressed", score === -1 ? "true" : "false");
    }
    return h("div", { class: "card rating-box" },
      h("h3", { class: "card-title", text: "¿Qué tal lo hizo?" }),
      h("p", { class: "muted", text: "Tu valoración entra en las métricas de la versión del agente y en lo que estudia el Coach." }),
      h("div", { class: "rating-row" }, up, down, note, save),
      r ? h("p", { class: "muted small" }, "Valorado ", ui.time(r.at), r.note ? " · «" + r.note + "»" : "") : null);
  }

  function resultTab(j, detail) {
    const parts = [];
    if (!O.isFinished(j.status)) {
      parts.push(ui.empty({
        cls: "empty-sm", icon: j.status === "en_cola" ? "🎫" : "⏳",
        title: j.status === "en_cola" ? "Esperando turno" : "Trabajando…",
        text: "El resultado aparecerá aquí cuando termine. Mientras, mira la pestaña En directo.",
      }));
      return parts;
    }
    if (j.verdict || j.score !== undefined && j.score !== null) {
      parts.push(h("div", { class: "verdict-row" }, ui.verdictBadge(j.verdict), ui.scoreBadge(j.score)));
    }
    if (j.summary) {
      parts.push(h("div", { class: "card summary-card" }, h("h3", { class: "card-title", text: "Resumen" }), O.md.render(j.summary)));
    }
    if (j.lessons && j.lessons.length) {
      const policy = S.snap.settings.auto_lessons;
      parts.push(h("div", { class: "card" },
        h("h3", { class: "card-title", text: "💡 Lecciones" }),
        h("p", { class: "muted small", text: policy === "aprobar" ? "Entran directamente en la memoria del proyecto." : policy === "off" ? "La Oficina ignora las lecciones (Ajustes)." : "Quedan como propuestas en la Bandeja hasta que las apruebes." }),
        h("ul", { class: "lessons" }, j.lessons.map((l) => h("li", { text: l })))));
    }
    if (O.isFinished(j.status) && j.status !== "cancelado") parts.push(ratingBox(j));
    if (!detail) {
      parts.push(ui.loading("Cargando el resultado…"));
    } else if (detail.result_text) {
      parts.push(h("div", { class: "card result-card" },
        h("div", { class: "card-head" }, h("h3", { class: "card-title", text: "Respuesta completa" }), h("span", { class: "spacer" }), ui.copyBtn(() => detail.result_text)),
        O.md.render(detail.result_text)));
    } else {
      parts.push(h("p", { class: "muted", text: "El agente no dejó una respuesta final." }));
    }
    return parts;
  }

  function changesTab(j, detail, patchState, load) {
    const ch = j.changes;
    if (!ch || !(ch.files > 0)) {
      const msg = !O.isFinished(j.status)
        ? (j.kind === "cambio" ? "Los cambios se recogen con git cuando el agente termina." : "Este tipo de trabajo no recoge cambios.")
        : ch && ch.error ? "La captura de cambios falló: " + ch.error
          : j.kind === "cambio" || j.kind === "revision" ? "No hubo cambios en el árbol de trabajo." : "Este tipo de trabajo no cambia código (lo explica o lo planifica).";
      return ui.empty({ cls: "empty-sm", icon: "🧩", text: msg });
    }
    const head = h("div", { class: "changes-head" },
      ui.diffStat(ch),
      ch.base_sha ? h("span", { class: "muted mono", title: "Commit base", text: "base " + ch.base_sha.slice(0, 10) }) : null,
      h("span", { class: "spacer" }),
      ui.btn("Descargar parche", { href: "/api/jobs/" + O.enc(j.id) + "/patch?download=1", icon: "download", size: "sm", download: j.id + ".patch" }));
    const notes = [];
    if (ch.patch_truncated) notes.push(h("div", { class: "notice notice-warn", text: "El parche superó el límite y está truncado: el visor muestra solo una parte." }));
    if (!ch.contents_complete) notes.push(h("div", { class: "notice", text: "No se guardaron los contenidos completos (demasiados ficheros o demasiado grandes): no se puede crear un PR desde la Oficina, pero sí descargar el parche." }));
    if (ch.error) notes.push(h("div", { class: "notice notice-bad", text: "Aviso de la captura: " + ch.error }));
    let body;
    if (patchState.error) body = h("p", { class: "field-error", text: patchState.error });
    else if (patchState.files) {
      body = patchState.files.length
        ? O.diff.render(patchState.files)
        : detail && detail.files && detail.files.length
          ? h("ul", { class: "diff-files" }, detail.files.map((f) => h("li", { class: "diff-jump" }, h("span", { class: "diff-status diff-st-" + f.status, text: f.status }), h("span", { class: "diff-path mono", text: f.path }), h("span", { class: "diff-plus", text: "+" + f.additions }), h("span", { class: "diff-minus", text: "−" + f.deletions }))))
          : h("p", { class: "muted", text: "El parche está vacío." });
    } else if (patchState.wanted) {
      body = ui.loading("Cargando el parche…");
      load();
    } else {
      body = ui.loading("Preparando el visor…");
    }
    return [head, notes, body];
  }

  function promptTab(detail) {
    if (!detail) return ui.loading();
    return h("div", { class: "prompt-tab" },
      h("p", { class: "muted", text: "Esto es exactamente lo que recibió el agente. El prompt compuesto añade el contexto de la Oficina (proyecto, memoria, pasos anteriores) a tu encargo." }),
      ui.preBlock("Encargo original", detail.prompt || "", { open: true }),
      ui.preBlock("Prompt compuesto (stdin del agente)", detail.final_prompt || "(aún no se ha compuesto)"),
      ui.preBlock("Persona (system prompt)", detail.system_prompt || "(sin persona)"));
  }

  // ---------------------------------------------------------------- view --

  O.route("/trabajo/:id", {
    title: "Trabajo",
    mount(root, params, query) {
      const id = params.id;
      let detail = null;
      let detailFor = "";
      const patchState = {};
      let loadingDetail = false;
      O.stream.watch("route", id);

      const head = h("div");
      const banner = h("div");
      const meta = h("div");
      const actions = h("div");
      const resultBox = h("div", { class: "tab-inner" });
      const changesBox = h("div", { class: "tab-inner" });
      const promptBox = h("div", { class: "tab-inner" });
      let timeline = null;
      const job = () => O.sel.job(id) || (detail && detail.id === id ? detail : null);

      const initial = job();
      const tabs = ui.tabs({
        tabs: [
          { id: "resultado", label: "📄 Resultado", render: () => resultBox },
          {
            id: "directo", label: "📡 En directo", render: () => {
              timeline = O.timeline({ jobId: id });
              return timeline.el;
            },
          },
          { id: "cambios", label: "🧩 Cambios", render: () => changesBox },
          { id: "prompt", label: "🧾 Prompt", render: () => promptBox },
        ],
        active: query.get("tab") || (initial && O.isFinished(initial.status) ? "resultado" : "directo"),
        onChange: (t) => {
          if (t === "cambios" && !patchState.wanted) {
            patchState.wanted = true;
            changesBox._sig = null;
            render(true);
          }
        },
      });
      // The patch (up to MiBs) is only fetched once the Cambios tab is open.
      if (tabs.current === "cambios") patchState.wanted = true;

      O.put(root, h("div", { class: "job-view" }, head, banner, meta, actions, tabs.el));

      const fetchDetail = async () => {
        if (loadingDetail) return;
        loadingDetail = true;
        try {
          const d = await O.api.get("/api/jobs/" + O.enc(id));
          detail = d;
          detailFor = d.status + "|" + JSON.stringify(d.changes || null) + "|" + (d.summary || "").length;
          if (!O.sel.job(id) && d.id) {
            const rec = Object.assign({}, d);
            for (const k of ["prompt", "final_prompt", "system_prompt", "result_text", "files", "can_pr"]) delete rec[k];
            O.state.jobs.set(rec.id, rec);
            O.state.jobsVersion++;
          }
          render(true);
        } catch (e) {
          if (e.status === 404 && !O.sel.job(id)) {
            O.put(root, ui.empty({ icon: "🔎", title: "Este trabajo no existe", text: "Puede que se borrara o que la retención lo eliminase.", actions: ui.btn("Ir al tablero", { href: "#/tablero", kind: "primary" }) }));
            dead = true;
          } else O.fail(e, "No se pudo cargar el trabajo");
        } finally {
          loadingDetail = false;
        }
      };
      let dead = false;

      const loadPatch = async () => {
        if (patchState.loading) return;
        patchState.loading = true;
        try {
          const text = await O.api.text("/api/jobs/" + O.enc(id) + "/patch");
          patchState.files = O.diff.parse(text);
          patchState.bytes = text.length;
        } catch (e) {
          patchState.error = e.status === 404 ? "No hay parche guardado." : e.message;
        } finally {
          patchState.loading = false;
          const j = job();
          if (j) O.put(changesBox, changesTab(j, detail, patchState, loadPatch));
        }
      };

      function render(force) {
        if (dead) return;
        const j = job();
        if (!j) {
          O.put(head, ui.loading("Cargando el trabajo…"));
          return;
        }
        const a = ui.agentOf(j);
        ui.memo(head, JSON.stringify([j.title, j.status, j.kind, j.priority, j.verdict, j.pr, a.name]), () => ui.pageHead({
          back: { href: "#/tablero", label: "Tablero" },
          title: h("span", { class: "job-title" }, ui.avatar(a, "md"), h("span", { text: j.title || "(sin título)" })),
          subtitle: h("span", { class: "chips" }, ui.statusPill(j.status), ui.kindBadge(j.kind), ui.verdictBadge(j.verdict), ui.scoreBadge(j.score), ui.prBadge(j.pr),
            h("span", { class: "muted mono", text: j.id })),
        }));
        O.router.setTitle(u.trunc(j.title || "Trabajo", 40));
        ui.memo(banner, JSON.stringify([j.status, j.activity, j.waiting, j.stalled, j.message, O.sel.queueJobs().map((x) => x.id).indexOf(j.id), S.snap.settings.queue_paused]), () => nowBanner(j));
        ui.memo(meta, JSON.stringify([j, O.sel.pipeline(j.pipeline_id) && O.sel.pipeline(j.pipeline_id).title]), () => metaGrid(j));
        ui.memo(actions, JSON.stringify([j.status, j.priority, j.rating, j.pr, j.changes, detail && detail.can_pr, O.sel.githubReady(j.project_id)]), () => actionsBar(j, detail));
        tabs.count("cambios", j.changes && j.changes.files ? j.changes.files : 0);
        // Re-fetch the detail when the job finished or its results changed.
        const want = j.status + "|" + JSON.stringify(j.changes || null) + "|" + (j.summary || "").length;
        if (!force && want !== detailFor && O.isFinished(j.status)) {
          detailFor = want;
          patchState.files = patchState.error = undefined;
          fetchDetail();
        }
        ui.memo(resultBox, JSON.stringify([j.status, j.summary, j.lessons, j.rating, j.verdict, j.score, detail ? (detail.result_text || "").length : -1]), () => resultTab(j, detail));
        ui.memo(changesBox, JSON.stringify([j.status, j.changes, !!patchState.files, patchState.error, detail ? 1 : 0, !!patchState.wanted]), () => changesTab(j, detail, patchState, loadPatch));
        ui.memo(promptBox, detail ? "d" + (detail.final_prompt || "").length : "none", () => promptTab(detail));
      }

      render(true);
      fetchDetail();
      return {
        title: initial ? u.trunc(initial.title || "Trabajo", 40) : "Trabajo",
        update(chg) {
          if (chg.full || !chg.jobs.length || chg.jobs.includes(id)) render(false);
        },
        unmount() {
          if (timeline) timeline.destroy();
        },
      };
    },
  });
})();
