/*
 * Oficina de agentes · actions shared by several views: job operations
 * (cancel, retry, rate, PR, comment, follow-up, priority, delete; PR and
 * comment show the exact GitHub text, editable, before publishing),
 * proposal decisions and the proposal card.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui, L } = O;
  const J = (id, op) => "/api/jobs/" + O.enc(id) + (op ? "/" + op : "");

  // A toast action that opens a GitHub URL in a new tab, only when the URL
  // passes O.safeLink (never javascript:, data:, "//host" or mangled URLs).
  function openAction(url) {
    const safe = typeof url === "string" ? O.safeLink(url) : null;
    if (!safe) return null;
    return { label: "Abrir en GitHub", fn: () => window.open(safe, "_blank", "noopener,noreferrer") };
  }

  // Applies a response that is a Job record.
  function takeJob(resp) {
    if (resp && typeof resp === "object" && resp.id && resp.status) {
      O.store.upsertJob(resp);
      return resp;
    }
    if (resp && resp.job && resp.job.id) {
      O.store.upsertJob(resp.job);
      return resp.job;
    }
    return null;
  }

  const act = (O.act = {});

  act.cancel = async function cancel(job, btn) {
    const running = O.isRunning(job.status);
    const ok = await O.confirm({
      title: running ? "¿Cancelar el trabajo en curso?" : "¿Quitar el trabajo de la cola?",
      text: running
        ? "El agente se detiene y la tarea del sandbox se borra. Se conserva el registro de lo que hizo."
        : "El trabajo no llegará a ejecutarse.",
      confirmLabel: running ? "Cancelar trabajo" : "Quitar de la cola",
      cancelLabel: "Volver",
      danger: true,
    });
    if (!ok) return null;
    return ui.busy(btn, async () => {
      const r = await O.api.post(J(job.id, "cancel"), {});
      takeJob(r);
      O.toast(running ? "Cancelando… el sandbox se limpia en unos segundos." : "Trabajo quitado de la cola.", { kind: "ok" });
      return r;
    }, "No se pudo cancelar");
  };

  act.retry = async function retry(job, btn, go) {
    return ui.busy(btn, async () => {
      const r = await O.api.post(J(job.id, "retry"), {});
      const nj = takeJob(r);
      O.toast("Trabajo puesto otra vez en la cola.", {
        kind: "ok",
        action: nj && !go ? { label: "Ver", fn: () => O.go("#/trabajo/" + O.enc(nj.id)) } : null,
      });
      if (nj && go) O.go("#/trabajo/" + O.enc(nj.id));
      return nj;
    }, "No se pudo reintentar");
  };

  act.remove = async function remove(job, btn) {
    const ok = await O.confirm({
      title: "¿Borrar este trabajo?",
      text: "Se borran su registro, su resultado y su parche. Las métricas ya calculadas dejan de contarlo.",
      confirmLabel: "Borrar", danger: true,
    });
    if (!ok) return false;
    return ui.busy(btn, async () => {
      await O.api.post(J(job.id, "delete"), {});
      O.store.removeJob(job.id);
      O.toast("Trabajo borrado.", { kind: "ok" });
      return true;
    }, "No se pudo borrar");
  };

  act.rate = async function rate(job, score, note, btn) {
    return ui.busy(btn, async () => {
      const r = await O.api.post(J(job.id, "rate"), { score, note: note || "" });
      if (!takeJob(r)) {
        O.store.upsertJob(Object.assign({}, job, { rating: { score, note: note || "", at: new Date(O.now()).toISOString() } }));
      }
      O.toast(score > 0 ? "Valorado 👍 — cuenta para las métricas de esta versión." : score < 0 ? "Valorado 👎 — el Coach lo tendrá en cuenta." : "Marcado como visto.", { kind: "ok" });
      return true;
    }, "No se pudo guardar la valoración");
  };

  act.priority = async function priority(job, p, btn) {
    return ui.busy(btn, async () => {
      const r = await O.api.post(J(job.id, "priority"), { priority: p });
      if (!takeJob(r)) O.store.upsertJob(Object.assign({}, job, { priority: p }));
      O.toast("Prioridad: " + (O.own(L.priority, p) || p) + ".", { kind: "ok", timeout: 2000 });
    }, "No se pudo cambiar la prioridad");
  };

  // ------------------------------------------------------------ GitHub --

  const MAX_GH_TITLE = 200;      // runes
  const MAX_GH_BODY = 60000;     // bytes

  /**
   * The exact text the Office would publish on GitHub for this job
   * (target "pr" or "comment"): {title, body}. A 409 means it does not
   * apply (no changes, no summary, PR already created…).
   */
  async function githubPreview(job, target) {
    const r = await O.api.get(J(job.id, "github-preview") + "?target=" + O.enc(target));
    return {
      title: r && typeof r.title === "string" ? r.title : "",
      body: r && typeof r.body === "string" ? r.body : "",
    };
  }

  const mentionsNote = () => h("div", { class: "notice notice-info small" },
    h("strong", { text: "Revisa el texto antes de publicarlo. " }),
    "Es lo que se publicará en GitHub con el token de la Oficina, salvo un detalle: las @menciones se neutralizan al publicar y no avisan a nadie.");

  /** Validates the edited title (PR only) and body; null when fine. */
  function checkGitHubText(form, title, body) {
    ui.fieldError(form, null);
    if (title) {
      const t = title.value.trim();
      if (!t) return { field: "title", message: "Ponle un título." };
      if (u.runes(t) > MAX_GH_TITLE) return { field: "title", message: "Máximo " + MAX_GH_TITLE + " caracteres." };
      if (/[\u0000-\u001f\u007f]/.test(t)) return { field: "title", message: "El título va en una sola línea." };
    }
    if (!body.value.trim()) return { field: "body", message: "El texto no puede quedar vacío." };
    if (u.bytes(body.value) > MAX_GH_BODY) return { field: "body", message: "Supera " + fmt.bytes(MAX_GH_BODY) + "." };
    return null;
  }

  /** Opens the PR dialog with the server's preview, editable. */
  act.createPR = async function createPR(job, btn) {
    const pv = await ui.busy(btn, () => githubPreview(job, "pr"), "No se pudo preparar el PR");
    if (!pv) return;
    const project = O.sel.project(job.project_id);
    const title = ui.input({ value: pv.title || job.title || "", maxlength: MAX_GH_TITLE, autofocus: true });
    const body = ui.textarea({ rows: 14, class: "input textarea mono gh-body", spellcheck: "false" });
    body.value = pv.body;
    const form = h("div", { class: "form gh-confirm" },
      h("p", { text: "La Oficina sube los ficheros cambiados a una rama nueva (oficina/" + job.id + ") de " + (project ? project.repo : "el repositorio") + " y abre un PR en borrador contra " + (job.branch || "la rama base") + ". El token de GitHub nunca entra en el sandbox." }),
      mentionsNote(),
      ui.field("Título del PR", title, { field: "title", counter: ui.counter(title, MAX_GH_TITLE, "runes") }),
      ui.field("Descripción (Markdown)", body, { field: "body", counter: ui.counter(body, MAX_GH_BODY), hint: "Es exactamente lo que se publicará; puedes editarlo." }));
    O.dialog({
      title: "Crear un pull request en borrador",
      wide: true,
      body: form,
      actions: [
        { label: "Cancelar", kind: "ghost" },
        {
          label: "Crear PR", kind: "primary",
          onClick: async () => {
            const bad = checkGitHubText(form, title, body);
            if (bad) {
              ui.fieldError(form, bad);
              return false;
            }
            try {
              const r = await O.api.post(J(job.id, "pr"), { title: title.value.trim(), body: body.value });
              const nj = takeJob(r);
              const pr = (nj && nj.pr) || (r && r.pr) || (r && r.url ? r : null);
              O.toast(pr && pr.number ? "PR #" + pr.number + " creado en borrador." : "PR creado.", {
                kind: "ok", timeout: 9000,
                action: pr ? openAction(pr.url) : null,
              });
              return true;
            } catch (e) {
              if (e.field === "title" || e.field === "body") ui.fieldError(form, e);
              else O.fail(e, "No se pudo crear el PR");
              return false;
            }
          },
        },
      ],
    });
  };

  /** Opens the comment dialog with the server's preview, editable. */
  act.comment = async function comment(job, btn) {
    const n = job.source && job.source.number;
    if (!n) return;
    const isPR = job.source.type === "pr";
    const where = (isPR ? "el PR" : "la issue") + " #" + n;
    const pv = await ui.busy(btn, () => githubPreview(job, "comment"), "No se pudo preparar el comentario");
    if (!pv) return;
    const body = ui.textarea({ rows: 12, class: "input textarea mono gh-body", spellcheck: "false", autofocus: true });
    body.value = pv.body;
    const form = h("div", { class: "form gh-confirm" },
      h("p", { text: "Se publica en " + where + " de " + O.sel.projectName(job.project_id) + " el resumen de este trabajo, con el agente y el modelo que lo hicieron." }),
      mentionsNote(),
      ui.field("Comentario (Markdown)", body, { field: "body", counter: ui.counter(body, MAX_GH_BODY), hint: "Es exactamente lo que se publicará; puedes editarlo." }));
    O.dialog({
      title: "Comentar en " + where,
      wide: true,
      body: form,
      actions: [
        { label: "Cancelar", kind: "ghost" },
        {
          label: "Publicar comentario", kind: "primary",
          onClick: async () => {
            const bad = checkGitHubText(form, null, body);
            if (bad) {
              ui.fieldError(form, bad);
              return false;
            }
            try {
              const r = await O.api.post(J(job.id, "comment"), { number: n, body: body.value });
              const url = r && typeof r.url === "string" ? r.url : "";
              O.toast("Comentario publicado en " + where + ".", {
                kind: "ok", timeout: 7000,
                action: openAction(url),
              });
              return true;
            } catch (e) {
              if (e.field === "body") ui.fieldError(form, e);
              else O.fail(e, "No se pudo comentar");
              return false;
            }
          },
        },
      ],
    });
  };

  /** Dialog: ask another agent to continue from this job. */
  act.followup = function followup(job) {
    const agents = O.sel.agents().filter((a) => a.enabled);
    const hasChanges = job.changes && job.changes.files > 0;
    const defKind = hasChanges && job.kind === "cambio" ? "revision" : "pregunta";
    const reviewer = agents.find((a) => a.id === "grace") || agents[0];
    const agentSel = ui.select(agents.map((a) => ({ value: a.id, label: a.emoji + " " + a.name + " — " + a.role })), (defKind === "revision" && reviewer ? reviewer.id : job.agent_id));
    const kinds = ["pregunta", "plan", "cambio", "revision"];
    const kindSel = ui.select(kinds.map((k) => ({ value: k, label: L.kinds[k].icon + " " + L.kinds[k].label })), defKind);
    const prompt = ui.textarea({ rows: 6, autofocus: true });
    const suggestions = {
      revision: "Revisa los cambios de este trabajo: corrección, seguridad, tests y estilo del proyecto.",
      cambio: "Continúa este trabajo: ",
      pregunta: "A partir del resultado anterior, ",
      plan: "Con lo aprendido en el trabajo anterior, planifica ",
    };
    prompt.value = suggestions[defKind];
    kindSel.addEventListener("change", () => {
      if (Object.values(suggestions).includes(prompt.value) || !prompt.value.trim()) prompt.value = suggestions[kindSel.value];
    });
    const form = h("div", null,
      h("p", { class: "muted", text: hasChanges
        ? "El nuevo trabajo empieza con los cambios de este ya aplicados en el árbol, y recibe su resultado como contexto."
        : "El nuevo trabajo recibe el resultado de este como contexto." }),
      h("div", { class: "grid-2" }, ui.field("Agente", agentSel, { field: "agent_id" }), ui.field("Tipo", kindSel, { field: "kind" })),
      ui.field("Encargo", prompt, { field: "prompt", counter: ui.counter(prompt, 32768) }));
    O.dialog({
      title: "Pedir a otro agente",
      body: form,
      actions: [
        { label: "Cancelar", kind: "ghost" },
        {
          label: "Encargar", kind: "primary",
          onClick: async () => {
            if (!prompt.value.trim()) {
              ui.fieldError(form, { field: "prompt", message: "Escribe qué tiene que hacer." });
              return false;
            }
            try {
              const r = await O.api.post(J(job.id, "followup"), { agent_id: agentSel.value, kind: kindSel.value, prompt: prompt.value });
              const nj = takeJob(r);
              O.toast("Encargo creado.", { kind: "ok" });
              if (nj) O.go("#/trabajo/" + O.enc(nj.id));
              return true;
            } catch (e) {
              ui.fieldError(form, e);
              return false;
            }
          },
        },
      ],
    });
  };

  // ------------------------------------------------------------ proposals --

  act.approve = async function approve(p, content, btn) {
    return ui.busy(btn, async () => {
      await O.api.post("/api/proposals/" + O.enc(p.id) + "/approve", content !== undefined && content !== null ? { content } : {});
      p.status = "aprobada";
      O.toast(p.type === "prompt" ? "Nuevo system prompt aprobado: el agente sube de versión." : "Lección aprobada: entra en la memoria del proyecto.", { kind: "ok" });
      O.store.sync();
      O.emit("store", { full: false, jobs: [] });
      return true;
    }, "No se pudo aprobar");
  };

  act.reject = async function reject(p, btn) {
    const ok = await O.confirm({ title: "¿Rechazar la propuesta?", text: "Queda en el historial como rechazada.", confirmLabel: "Rechazar", danger: true });
    if (!ok) return false;
    return ui.busy(btn, async () => {
      await O.api.post("/api/proposals/" + O.enc(p.id) + "/reject", {});
      p.status = "rechazada";
      O.toast("Propuesta rechazada.", { kind: "ok" });
      O.store.sync();
      O.emit("store", { full: false, jobs: [] });
      return true;
    }, "No se pudo rechazar");
  };

  /** A card for a proposal, with approve / edit / reject. */
  ui.proposalCard = function proposalCard(p, state) {
    const st = state || {};
    const isPrompt = p.type === "prompt";
    const target = isPrompt ? O.sel.agent(p.target_id) : O.sel.project(p.target_id);
    const job = p.source_job ? O.sel.job(p.source_job) : null;
    const pending = p.status === "pendiente";
    const editor = ui.textarea({ rows: isPrompt ? 14 : 4, class: "input textarea mono", "aria-label": "Contenido editado" });
    editor.value = st.draft !== undefined ? st.draft : p.content;
    // The preview (lesson text or prompt diff) shows what would be approved
    // (the edit while editing), with invisible characters as ⟦U+XXXX⟧.
    const shown = () => (st.editing ? editor.value : p.content);
    const preview = h("div", { class: "proposal-preview" });
    const drawPreview = () => {
      if (isPrompt) O.put(preview, O.diff.renderLines(O.diff.lines(target ? target.system_prompt || "" : "", shown())));
      else O.put(preview, h("blockquote", { class: "proposal-lesson", text: O.visible(shown()) }));
    };
    const redraw = u.debounce(drawPreview, 200);
    editor.addEventListener("input", () => {
      st.draft = editor.value;
      redraw();
    });
    const editNote = ui.invisibleNote(editor);
    const editWrap = h("div", { class: "proposal-edit", hidden: !st.editing },
      ui.field(isPrompt ? "System prompt (puedes retocarlo antes de aprobar)" : "Lección (puedes retocarla antes de aprobar)", editor,
        { counter: ui.counter(editor, isPrompt ? 16384 : 600, isPrompt ? "bytes" : "runes") }),
      editNote);
    const contentWarn = O.invisibleWarning(p.content);
    const body = [];
    if (contentWarn) body.push(h("p", { class: "notice notice-warn invisible-note", role: "note", text: "⚠️ " + contentWarn + " Se muestran como ⟦U+…⟧." }));
    drawPreview();
    if (isPrompt) {
      body.push(p.rationale ? h("div", { class: "proposal-why" }, h("strong", { text: "Motivo: " }), O.md.render(p.rationale, "md-compact")) : null);
      body.push(h("details", { class: "proposal-diff", open: st.showDiff ? true : null },
        h("summary", null, "Cambios respecto a v" + (target ? target.version : "?")),
        preview));
    } else {
      body.push(preview);
      if (p.rationale) body.push(h("p", { class: "muted", text: O.visible(p.rationale) }));
    }
    const buttons = () => [
      ui.btn(st.editing ? "Aprobar con cambios" : "Aprobar", {
        kind: "primary", size: "sm", icon: "check",
        onClick: (e) => act.approve(p, st.editing && editor.value !== p.content ? editor.value : undefined, e.currentTarget),
      }),
      ui.btn(st.editing ? "Descartar edición" : "Editar", {
        size: "sm", icon: "edit",
        onClick: () => {
          st.editing = !st.editing;
          editWrap.hidden = !st.editing;
          if (!st.editing) {
            st.draft = undefined;
            editor.value = p.content;
          } else editor.focus();
          editNote.refresh();
          drawPreview();
          O.put(actions, buttons());
        },
      }),
      ui.btn("Rechazar", { size: "sm", kind: "danger-ghost", onClick: (e) => act.reject(p, e.currentTarget) }),
    ];
    const actions = pending ? h("div", { class: "row-actions" }, buttons()) : null;
    return h("article", { class: ["card", "proposal", "is-" + p.status] },
      h("header", { class: "card-head" },
        h("span", { class: "card-icon", "aria-hidden": "true", text: isPrompt ? "🧭" : "💡" }),
        h("div", { class: "card-head-text" },
          h("h3", { class: "card-title" }, isPrompt
            ? ["Nuevo system prompt para ", target ? h("a", { href: "#/agentes/" + O.enc(target.id), text: target.emoji + " " + target.name }) : p.target_id]
            : ["Lección para ", target ? h("a", { href: "#/proyectos/" + O.enc(target.id), text: target.name }) : p.target_id]),
          h("p", { class: "card-sub muted" },
            ui.time(p.created),
            job ? [" · de ", h("a", { href: "#/trabajo/" + O.enc(job.id), text: u.trunc(job.title || job.id, 50) })] : p.source_job ? " · trabajo " + p.source_job : null)),
        !pending ? ui.statusPill(p.status === "aprobada" ? "hecho" : "cancelado", { label: O.own(L.status, p.status) || p.status }) : null),
      body, editWrap, actions);
  };

})();
