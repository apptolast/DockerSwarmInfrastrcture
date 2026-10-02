/*
 * Oficina de agentes · Nuevo (#/nuevo): commission a job for one agent or
 * a team (pipeline). Query: tab=equipo, agente, proyecto, tipo, issue, pr,
 * plantilla, desde (source job).
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui, L } = O;
  const S = O.state;

  const QUICK = [
    { icon: "🏛️", label: "Explica la arquitectura", kind: "pregunta", text: "Explica la arquitectura de este repositorio: componentes principales, cómo fluyen los datos, dependencias externas, cómo se despliega y por dónde empezaría alguien nuevo. Cita ficheros y líneas." },
    { icon: "🐛", label: "Busca bugs", kind: "pregunta", text: "Busca bugs reales (no estilo): condiciones de carrera, errores sin manejar, casos límite, fugas de recursos, validaciones que faltan. Para cada uno: fichero:línea, por qué falla, cómo reproducirlo y el arreglo propuesto. Ordena por gravedad." },
    { icon: "🛡️", label: "Auditoría de seguridad", kind: "pregunta", text: "Haz una auditoría de seguridad: entradas sin validar, inyección, secretos en el repositorio, permisos y autenticación, dependencias vulnerables, configuración insegura (contenedores, CI, cabeceras). Clasifica por gravedad con fichero:línea y arreglo propuesto." },
    { icon: "🧪", label: "Escribe tests", kind: "cambio", text: "Añade tests para las partes con más riesgo y menos cobertura. Sigue el framework y el estilo que ya usa el proyecto, ejecútalos y deja todo en verde. Explica qué cubren." },
    { icon: "📦", label: "Actualiza dependencias", kind: "cambio", text: "Actualiza las dependencias a sus últimas versiones compatibles (sin saltos de versión mayor salvo que sean triviales), ajusta el código si hace falta y verifica que build y tests pasan. Resume qué cambió y los riesgos." },
    { icon: "📚", label: "Mejora la documentación", kind: "cambio", text: "Mejora la documentación: README con propósito, requisitos, instalación, uso y despliegue; comenta lo que no sea obvio. No inventes nada: compruébalo en el código." },
    { icon: "🗺️", label: "Planifica una mejora", kind: "plan", text: "Propón un plan para mejorar " },
    { icon: "🎯", label: "Implementa la issue…", kind: "cambio", issue: true, text: "Implementa la issue #" },
  ];

  let lastProject = "";

  // Issue and PR titles and bodies are written by anyone on GitHub: the
  // server attaches them as framed untrusted data, so the prompt (and the
  // job title, which later steps see as context) only names the number.
  const NEUTRAL = {
    issue: (n) => "Implementa la issue #" + n + ". Su título y su descripción van adjuntos como datos externos.",
    pr: (n) => "Revisa el PR #" + n + ": corrección, seguridad, tests y coherencia con el proyecto. Su título, su descripción y su diff van adjuntos como datos externos.",
  };
  const NEUTRAL_TITLE = {
    issue: (n) => "Issue #" + n,
    pr: (n) => "Revisión del PR #" + n,
  };
  const untrustedHint = (type) => h("p", { class: "field-hint source-hint" },
    "🔒 El título y la descripción " + (type === "pr" ? "del PR (y su diff)" : "de la issue") +
    " se adjuntan como datos externos no confiables: el agente los lee como datos, nunca como instrucciones.");

  function projectInfo(p) {
    if (!p) return null;
    return h("p", { class: "field-hint" },
      p.description ? p.description + " " : "",
      h("a", { href: p.repo, target: "_blank", rel: "noopener noreferrer", class: "mono" }, p.repo.replace(/^https:\/\//, "")),
      " · rama ", h("code", { text: p.branch || "main" }));
  }

  function projectOptions(selected) {
    return O.sel.projects().filter((p) => !p.archived || p.id === selected).map((p) => ({ value: p.id, label: p.name }));
  }

  // ----------------------------------------------------- GitHub picker --

  function githubPicker(projectId, onPick, only) {
    const box = h("div", { class: "gh-picker" }, ui.loading("Leyendo issues y PRs de GitHub…"));
    O.github(projectId).then((d) => {
      if (!d.enabled) {
        O.put(box, h("p", { class: "muted", text: "GitHub no está configurado para este proyecto." }));
        return;
      }
      const issues = only === "pr" ? [] : d.issues;
      const pulls = only === "issue" ? [] : d.pulls;
      if (!issues.length && !pulls.length) {
        O.put(box, h("p", { class: "muted", text: "No hay issues ni PRs abiertos." }));
        return;
      }
      const item = (type, x) => h("li", null, h("button", {
        type: "button", class: "gh-item", on: { click: () => onPick(type, x) },
      },
      h("span", { class: "gh-num mono", text: (type === "pr" ? "PR #" : "#") + x.number }),
      h("span", { class: "gh-title", text: x.title || "" }),
      type === "issue" ? ghLabels(x) : h("span", { class: "muted mono", text: ghRef(x.head) + " → " + ghRef(x.base) })));
      O.put(box,
        issues.length ? [h("h4", { class: "gh-h", text: "Issues abiertas" }), h("ul", { class: "gh-list" }, issues.map((x) => item("issue", x)))] : null,
        pulls.length ? [h("h4", { class: "gh-h", text: "Pull requests abiertos" }), h("ul", { class: "gh-list" }, pulls.map((x) => item("pr", x)))] : null);
    }).catch((e) => O.put(box, h("p", { class: "field-error", text: e.message })));
    return box;
  }
  const ghRef = (r) => (r && typeof r === "object" ? r.ref || r.label || "" : r || "");
  function ghLabels(x) {
    const labels = (x.labels || []).map((l) => (typeof l === "string" ? l : l && l.name)).filter(Boolean);
    return labels.length ? h("span", { class: "gh-labels" }, labels.slice(0, 4).map((l) => h("span", { class: "chip", text: l }))) : null;
  }
  O.ghLabels = ghLabels;
  O.ghRef = ghRef;
  O.ghUser = (x) => (x && x.user && typeof x.user === "object" ? x.user.login : x && (x.user || x.author)) || "";

  // ----------------------------------------------------------- job form --

  function jobForm(query) {
    const snap = S.snap;
    const lim = snap.limits || {};
    const maxPrompt = Math.min(lim.max_prompt_bytes || 32768, 32768);
    const agents = O.sel.agents().filter((a) => a.enabled);
    if (!agents.length) {
      return ui.empty({ icon: "🪑", title: "No hay agentes activos", text: "Activa o crea un agente para poder encargarle trabajo.", actions: ui.btn("Ir a Agentes", { href: "#/agentes", kind: "primary" }) });
    }
    const projects = projectOptions(query.get("proyecto"));
    if (!projects.length) {
      return ui.empty({ icon: "📁", title: "No hay proyectos", text: "Añade un proyecto (un repositorio público de GitHub) para empezar.", actions: ui.btn("Ir a Proyectos", { href: "#/proyectos", kind: "primary" }) });
    }
    const st = {
      project: query.get("proyecto") || lastProject || (O.sel.project(snap.settings.retro_project) && !O.sel.project(snap.settings.retro_project).archived ? snap.settings.retro_project : projects[0].value),
      agent: query.get("agente") || snap.settings.default_agent,
      kind: query.get("tipo") || "",
      source: null,
      priority: 1,
      mode: "",
    };
    if (!projects.some((p) => p.value === st.project)) st.project = projects[0].value;
    if (!agents.some((a) => a.id === st.agent)) st.agent = agents[0].id;
    const agentOf = () => O.sel.agent(st.agent) || agents[0];
    if (!L.kinds[st.kind] || st.kind === "retro" || st.kind === "juez") st.kind = agentOf().mode === "completo" ? "cambio" : "pregunta";

    const form = h("form", { class: "form", novalidate: true });
    // project
    const projSel = ui.select(projects, st.project);
    const projHint = h("div");
    const ghBox = h("div");
    // agents
    const agentGrid = h("div", { class: "agent-pick", role: "group", "aria-label": "Agente" });
    // kind
    const kindSeg = ui.segmented({
      label: "Tipo de trabajo", showHint: true,
      options: ["pregunta", "plan", "cambio", "revision"].map((k) => ({ value: k, label: L.kinds[k].label, icon: L.kinds[k].icon, hint: L.kinds[k].hint })),
      value: st.kind,
      onChange: (v) => { st.kind = v; checkMode(); },
    });
    const modeWarn = h("div", { class: "notice notice-warn", hidden: true });
    // prompt
    const prompt = ui.textarea({ rows: 8, placeholder: "Describe el encargo con el máximo contexto: qué quieres, dónde mirar, cómo sabrás que está bien…", required: true });
    const sourceChip = h("div", { class: "source-chip-row" });
    const quick = h("div", { class: "chips quick" }, QUICK.map((q) => h("button", {
      type: "button", class: "chip chip-btn",
      on: {
        click: () => {
          if (q.issue && O.sel.githubReady(st.project)) {
            openGitHub("issue");
            return;
          }
          if (prompt.value.trim() && !QUICK.some((x) => x.text === prompt.value)) {
            prompt.value = q.text + "\n\n" + prompt.value;
          } else prompt.value = q.text;
          prompt.dispatchEvent(new Event("input"));
          kindSeg.set(q.kind, true);
          prompt.focus();
          prompt.setSelectionRange(prompt.value.length, prompt.value.length);
        },
      },
    }, h("span", { "aria-hidden": "true", text: q.icon + " " }), q.label)));
    const title = ui.input({ maxlength: 120, placeholder: "Se genera a partir del encargo si lo dejas vacío" });
    const branch = ui.input({ maxlength: 200, autocomplete: "off", spellcheck: "false", class: "input mono" });
    const prio = ui.segmented({
      label: "Prioridad",
      options: [{ value: 0, label: "Baja" }, { value: 1, label: "Normal" }, { value: 2, label: "Alta", icon: "🔥" }],
      value: 1, onChange: (v) => { st.priority = Number(v); },
    });
    // advanced
    const advBox = h("div");
    let picker = null, modeSeg = null, turns = null, timeout = null;
    const summary = h("p", { class: "form-summary" });

    function drawProject() {
      const p = O.sel.project(st.project);
      O.put(projHint, projectInfo(p));
      branch.placeholder = (p && p.branch) || "main";
      O.put(ghBox, O.sel.githubReady(st.project)
        ? ui.btn("Desde una issue o un PR de GitHub…", { size: "sm", icon: "branch", kind: "ghost", onClick: () => openGitHub() })
        : null);
    }

    function openGitHub(only) {
      const d = O.dialog({
        title: "Elegir de GitHub", wide: true,
        body: githubPicker(st.project, (type, x) => {
          setSource(type, x);
          d.close();
        }, only),
        actions: [{ label: "Cerrar", kind: "ghost" }],
      });
    }

    // Texts this form wrote itself, replaced freely when the source changes.
    let autoPrompt = "", autoTitle = "";
    function setSource(type, x) {
      const n = x ? Number(x.number) : 0;
      st.source = x && Number.isInteger(n) && n > 0 ? { type: type === "pr" ? "pr" : "issue", number: n } : null;
      const ownPrompt = !prompt.value.trim() || prompt.value === autoPrompt || QUICK.some((q) => q.text === prompt.value);
      const ownTitle = !title.value.trim() || title.value === autoTitle;
      if (st.source) {
        kindSeg.set(st.source.type === "issue" ? "cambio" : "revision", true);
        // Only the number: never the attacker-controlled title.
        if (ownPrompt) prompt.value = autoPrompt = NEUTRAL[st.source.type](n);
        if (ownTitle) title.value = autoTitle = NEUTRAL_TITLE[st.source.type](n);
      } else {
        if (ownPrompt && autoPrompt && prompt.value === autoPrompt) prompt.value = "";
        if (ownTitle && autoTitle && title.value === autoTitle) title.value = "";
        autoPrompt = autoTitle = "";
      }
      prompt.dispatchEvent(new Event("input"));
      // The title is shown to the human as plain text only.
      O.put(sourceChip, st.source ? [h("span", { class: "chip chip-source" },
        O.icon("branch", { size: 14 }), (st.source.type === "pr" ? " PR #" : " Issue #") + n + (x.title ? " · " + u.trunc(x.title, 50) : ""),
        h("button", { type: "button", class: "chip-x", "aria-label": "Quitar origen", on: { click: () => setSource(null, null) } }, "×")),
      untrustedHint(st.source.type)] : null);
    }

    function drawAgents() {
      O.put(agentGrid, agents.map((a) => {
        const ast = O.sel.agentState(a.id);
        const on = a.id === st.agent;
        return h("button", {
          type: "button", class: ["agent-card", on && "is-on"], "aria-pressed": on ? "true" : "false",
          style: { "--agent": u.color(a.color) },
          on: { click: () => { st.agent = a.id; drawAgents(); drawAdvanced(); checkMode(); } },
        },
        ui.avatar(a, "md"),
        h("span", { class: "agent-card-text" },
          h("strong", { text: a.name }),
          h("span", { class: "agent-card-role", text: a.role }),
          h("span", { class: "agent-card-model mono", text: ui.modelLabel(a.harness, a.model, a.effort) })),
        h("span", { class: ["agent-card-mode", a.mode === "completo" ? "is-full" : ""], title: (L.modes[a.mode] || {}).hint || null, text: a.mode === "completo" ? "completo" : "lectura" }),
        ast.state === "working" ? h("span", { class: "agent-card-busy", text: "trabajando" }) : ast.queued.length ? h("span", { class: "agent-card-busy", text: ast.queued.length + " en cola" }) : null);
      }));
    }

    function drawAdvanced() {
      const a = agentOf();
      st.mode = a.mode;
      picker = ui.modelPicker({ harness: a.harness, model: a.model, effort: a.effort, fallback_model: a.fallback_model }, { pickFirst: true, onChange: () => { syncTurns(); updateSummary(); } });
      modeSeg = ui.segmented({
        label: "Modo", showHint: true,
        options: ["lectura", "completo"].map((m) => ({ value: m, label: L.modes[m].label, hint: L.modes[m].hint })),
        value: a.mode, onChange: (v) => { st.mode = v; checkMode(); },
      });
      turns = ui.input({ type: "number", min: 1, max: lim.max_turns || 500, value: a.max_turns || 40, inputmode: "numeric" });
      timeout = ui.input({ type: "number", min: 5, max: lim.max_timeout_minutes || 180, value: a.timeout_minutes || 30, inputmode: "numeric" });
      O.put(advBox, h("details", { class: "advanced" },
        h("summary", null, "Ajustes avanzados ", h("span", { class: "muted", text: "· solo para este encargo" })),
        h("div", { class: "advanced-body" },
          h("p", { class: "field-hint", text: "Parte de la configuración de " + a.name + " (v" + a.version + "). Lo que cambies aquí no modifica al agente." }),
          picker.el,
          ui.field("Modo", modeSeg.el, { field: "mode" }),
          h("div", { class: "grid-2" },
            ui.field("Turnos máximos", turns, { field: "max_turns", hint: "Claude: 1-" + (lim.max_turns || 500) + ". Codex no tiene límite de turnos.", cls: "turns-field" }),
            ui.field("Tiempo máximo (min)", timeout, { field: "timeout_minutes", hint: "5-" + (lim.max_timeout_minutes || 180) + " minutos." })))));
      syncTurns();
    }
    function syncTurns() {
      const tf = advBox.querySelector(".turns-field");
      if (tf && picker) tf.hidden = picker.value().harness !== "claude";
    }

    function effectiveMode() {
      return st.mode || agentOf().mode;
    }
    function checkMode() {
      const bad = st.kind === "cambio" && effectiveMode() !== "completo";
      modeWarn.hidden = !bad;
      if (bad) {
        O.put(modeWarn,
          h("span", { text: "⚠️ " + agentOf().name + " trabaja en modo lectura y un cambio necesita modo completo. " }),
          h("button", { type: "button", class: "link-btn", on: { click: () => { modeSeg.set("completo", true); } } }, "Usar modo completo en este encargo"),
          " o elige otro agente.");
      }
      updateSummary();
    }

    function updateSummary() {
      const a = agentOf();
      const p = O.sel.project(st.project);
      const qn = O.sel.queueJobs().length;
      const busy = !!S.snap.active || O.sel.running().length > 0;
      const mp = picker ? picker.value() : { harness: a.harness, model: a.model, effort: a.effort };
      summary.textContent = a.emoji + " " + a.name + " (" + ui.modelLabel(mp.harness, mp.model, mp.effort) + ", " + effectiveMode() + ") trabajará en " +
        (p ? p.name : "?") + ", rama " + (branch.value.trim() || (p && p.branch) || "main") + ". " +
        (S.snap.settings.queue_paused ? "La cola está en pausa: esperará a que la reanudes." : busy || qn ? "Entrará en la cola en la posición " + (qn + 1) + " (según prioridad)." : "El sandbox está libre: empezará enseguida.");
    }

    projSel.addEventListener("change", () => {
      st.project = projSel.value;
      lastProject = st.project;
      setSource(null, null);
      drawProject();
      updateSummary();
    });
    branch.addEventListener("input", updateSummary);

    const submit = ui.btn("Encargar", { type: "submit", kind: "primary", icon: "send", cls: "btn-lg" });
    O.put(form,
      h("div", { class: "form-grid" },
        h("div", { class: "form-main" },
          ui.field("Proyecto", projSel, { field: "project_id" }), projHint,
          h("div", { class: "field", dataset: { field: "agent_id" } }, h("span", { class: "field-label", text: "Agente" }), agentGrid, h("p", { class: "field-error", role: "alert" })),
          ui.field("Tipo de trabajo", kindSeg.el, { field: "kind" }), modeWarn,
          h("div", { class: "field", dataset: { field: "prompt" } },
            h("div", { class: "field-label-row" }, h("label", { class: "field-label", for: prompt.id || (prompt.id = u.uid("f")), text: "Encargo" }), ghBox),
            h("div", { class: "quick-wrap" }, h("span", { class: "muted quick-label", text: "Plantillas rápidas:" }), quick),
            sourceChip, prompt, ui.counter(prompt, maxPrompt),
            h("p", { class: "field-error", role: "alert" })),
          h("div", { class: "grid-2" },
            ui.field("Título", title, { field: "title", optional: true }),
            ui.field("Rama", branch, { field: "branch", optional: true, hint: "Rama de la que se clona. Por defecto, la del proyecto." })),
          ui.field("Prioridad", prio.el, { field: "priority" }),
          advBox,
          h("div", { class: "form-foot" }, summary, submit))));

    drawProject();
    drawAgents();
    drawAdvanced();
    checkMode();
    const issueN = Number(query.get("issue")), prN = Number(query.get("pr"));
    if ((issueN || prN) && O.sel.githubReady(st.project)) {
      O.github(st.project).then((d) => {
        const x = issueN ? d.issues.find((i) => i.number === issueN) : d.pulls.find((i) => i.number === prN);
        setSource(issueN ? "issue" : "pr", x || { number: issueN || prN, title: "" });
      }).catch(() => setSource(issueN ? "issue" : "pr", { number: issueN || prN, title: "" }));
    } else if (issueN || prN) {
      setSource(issueN ? "issue" : "pr", { number: issueN || prN, title: "" });
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ui.fieldError(form, null);
      const a = agentOf();
      const err = (field, message) => { ui.fieldError(form, { field, message }); return false; };
      if (!prompt.value.trim()) return err("prompt", "Escribe el encargo.");
      if (u.bytes(prompt.value) > maxPrompt) return err("prompt", "El encargo supera " + fmt.bytes(maxPrompt) + ".");
      if (u.runes(title.value) > 120) return err("title", "Máximo 120 caracteres.");
      if (!ui.valid.branch(branch.value.trim())) return err("branch", "Nombre de rama no válido.");
      const pv = picker.validate();
      if (pv) {
        advBox.querySelector("details").open = true;
        return err(pv.field, pv.message);
      }
      const mp = picker.value();
      const tn = Number(turns.value), tm = Number(timeout.value);
      if (mp.harness === "claude" && !(Number.isInteger(tn) && tn >= 1 && tn <= (lim.max_turns || 500))) {
        advBox.querySelector("details").open = true;
        return err("max_turns", "Entre 1 y " + (lim.max_turns || 500) + ".");
      }
      if (!(Number.isInteger(tm) && tm >= 5 && tm <= (lim.max_timeout_minutes || 180))) {
        advBox.querySelector("details").open = true;
        return err("timeout_minutes", "Entre 5 y " + (lim.max_timeout_minutes || 180) + " minutos.");
      }
      if (st.kind === "cambio" && effectiveMode() !== "completo") return err("kind", "Un trabajo de tipo cambio necesita modo completo.");
      if (mp.harness !== a.harness && !mp.model) {
        advBox.querySelector("details").open = true;
        return err("model", "Elige un modelo de " + (L.harness[mp.harness] || mp.harness) + ": el de " + a.name + " es de otro motor.");
      }
      const ov = {};
      if (mp.harness !== a.harness) ov.harness = mp.harness;
      if (mp.model !== (a.model || "") || ov.harness) ov.model = mp.model;
      if (mp.effort !== (a.effort || "") || ov.harness) ov.effort = mp.effort;
      if (mp.harness === "claude" && mp.fallback_model !== (a.fallback_model || "")) ov.fallback_model = mp.fallback_model;
      if (effectiveMode() !== a.mode) ov.mode = effectiveMode();
      if (mp.harness === "claude" && tn !== a.max_turns) ov.max_turns = tn;
      if (tm !== a.timeout_minutes) ov.timeout_minutes = tm;
      for (const k of Object.keys(ov)) if (ov[k] === "" || ov[k] === 0) delete ov[k];
      const body = {
        project_id: st.project, agent_id: a.id, kind: st.kind, prompt: prompt.value,
        priority: st.priority,
      };
      if (title.value.trim()) body.title = title.value.trim();
      if (branch.value.trim()) body.branch = branch.value.trim();
      if (Object.keys(ov).length) body.overrides = ov;
      if (st.source) body.source = st.source;
      await ui.busy(submit, async () => {
        try {
          const job = await O.api.post("/api/jobs", body);
          lastProject = st.project;
          if (job && job.id) {
            O.store.upsertJob(job);
            O.toast("Encargo creado: " + (job.title || ""), { kind: "ok" });
            O.go("#/trabajo/" + O.enc(job.id));
          }
        } catch (ex) {
          ui.fieldError(form, ex);
        }
      });
    });
    return form;
  }

  // ---------------------------------------------------------- team form --

  function teamForm(query) {
    const snap = S.snap;
    const templates = snap.templates.filter((t) => t.id !== "evaluacion");
    if (!templates.length) return ui.empty({ icon: "🧑‍🤝‍🧑", title: "No hay plantillas de equipo", text: "El servidor no ha publicado plantillas." });
    const agents = O.sel.agents().filter((a) => a.enabled);
    const projects = projectOptions(query.get("proyecto"));
    if (!projects.length) return ui.empty({ icon: "📁", title: "No hay proyectos", actions: ui.btn("Ir a Proyectos", { href: "#/proyectos", kind: "primary" }) });
    const qIssue = Number(query.get("issue")), qPR = Number(query.get("pr"));
    const st = {
      template: templates.some((t) => t.id === query.get("plantilla")) ? query.get("plantilla") : templates[0].id,
      project: query.get("proyecto") || lastProject || projects[0].value,
      priority: 1,
      sourceType: "",
      // An issue to work on ("Asignar a… un equipo"): only its number is
      // sent; the server attaches its text as untrusted data.
      issue: Number.isInteger(qIssue) && qIssue > 0 ? qIssue : 0,
    };
    if (!projects.some((p) => p.value === st.project)) st.project = projects[0].value;
    const tplGrid = h("div", { class: "tpl-grid", role: "group", "aria-label": "Plantilla de equipo" });
    const detail = h("div");

    function drawTemplates() {
      O.put(tplGrid, templates.map((t) => {
        const on = t.id === st.template;
        return h("button", {
          type: "button", class: ["tpl-card", on && "is-on"], "aria-pressed": on ? "true" : "false",
          on: { click: () => { st.template = t.id; drawTemplates(); drawDetail(); } },
        },
        h("span", { class: "tpl-icon", "aria-hidden": "true", text: t.icon || "🧑‍🤝‍🧑" }),
        h("strong", { class: "tpl-name", text: t.name }),
        h("span", { class: "tpl-desc", text: t.description || "" }),
        h("span", { class: "tpl-roles" }, t.roles.map((r) => {
          const a = O.sel.agent(r.default_agent);
          return h("span", { class: "tpl-role", title: r.label }, a ? a.emoji : "•", " " + r.label);
        })),
        t.iterative ? h("span", { class: "badge badge-soft", text: "🔁 iterativo" }) : null);
      }));
    }

    function drawDetail() {
      const t = O.sel.template(st.template);
      const form = h("form", { class: "form", novalidate: true });
      if (t.id === "mejora" && O.sel.project(snap.settings.retro_project)) st.project = snap.settings.retro_project;
      const projSel = ui.select(projects, st.project);
      projSel.addEventListener("change", () => {
        st.project = projSel.value;
        lastProject = st.project;
        // The issue number belongs to the previous project's repository.
        if (st.issue) dropIssue();
        drawSource();
      });
      const seats = t.roles.map((r) => {
        const sel = ui.select(agents.map((a) => ({ value: a.id, label: a.emoji + " " + a.name + " — " + a.role })), r.default_agent);
        if (!agents.some((a) => a.id === r.default_agent) && agents[0]) sel.value = agents[0].id;
        return { r, sel };
      });
      const task = ui.textarea({ rows: 7, placeholder: "Qué tiene que conseguir el equipo. Cada paso recibe este encargo y el resultado de los anteriores." });
      if (t.id === "mejora") {
        task.value = "Mejora la Oficina de agentes (images/ax-web): elige UNA mejora concreta y valiosa (fiabilidad, claridad de la interfaz, tests o rendimiento), justifícala e impleméntala con tests. Respeta las reglas del repositorio.";
      }
      const maxIt = ui.select([1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: n + " " + u.plural(n, "ronda", "rondas") })), String(snap.settings.max_iterations || 2));
      const title = ui.input({ maxlength: 120, placeholder: t.name });
      const branch = ui.input({ maxlength: 200, class: "input mono", autocomplete: "off", spellcheck: "false" });
      const prio = ui.segmented({ label: "Prioridad", options: [{ value: 0, label: "Baja" }, { value: 1, label: "Normal" }, { value: 2, label: "Alta", icon: "🔥" }], value: 1, onChange: (v) => { st.priority = Number(v); } });
      const sourceBox = h("div");
      let sourceSel = null, prInput = null;
      const prQuery = Number.isInteger(qPR) && qPR > 0;
      st.sourceType = t.needs === "pr" || (t.needs && prQuery) ? "pr" : t.needs === "patch" ? "job" : "";
      // Neutral task text written by the form (the number only), replaced
      // when the issue or PR changes and the person has not edited it.
      let autoTask = "";
      const setAutoTask = (text) => {
        if (!task.value.trim() || task.value === autoTask) {
          task.value = autoTask = text;
          task.dispatchEvent(new Event("input"));
        }
      };
      // The self-improvement team works on its own repository.
      const issueHere = () => !t.needs && t.id !== "mejora" && st.issue > 0;
      function dropIssue() {
        if (task.value === autoTask) {
          task.value = "";
          task.dispatchEvent(new Event("input"));
        }
        autoTask = "";
        st.issue = 0;
      }
      if (issueHere()) setAutoTask(NEUTRAL.issue(st.issue));
      else if (st.sourceType === "pr" && prQuery) setAutoTask(NEUTRAL.pr(qPR));

      function drawSource() {
        const p = O.sel.project(st.project);
        branch.placeholder = (p && p.branch) || "main";
        if (!t.needs) {
          O.put(sourceBox, issueHere() ? h("div", { class: "field", dataset: { field: "source" } },
            h("span", { class: "field-label", text: "Fuente" }),
            h("div", { class: "source-chip-row" },
              h("span", { class: "chip chip-source" }, O.icon("branch", { size: 14 }), " Issue #" + st.issue,
                h("button", {
                  type: "button", class: "chip-x", "aria-label": "Quitar la issue",
                  on: { click: () => { dropIssue(); drawSource(); } },
                }, "×")),
              untrustedHint("issue")),
            h("p", { class: "field-error", role: "alert" })) : null);
          return;
        }
        const typeSeg = ui.segmented({
          label: "Qué revisar",
          options: [{ value: "job", label: "Cambios de un trabajo" }, { value: "pr", label: "Un PR de GitHub" }, { value: "", label: "Nada (todo el repo)" }],
          value: st.sourceType, onChange: (v) => { st.sourceType = v; drawSource(); },
        });
        let ctl = null;
        if (st.sourceType === "job") {
          const jobs = O.sel.jobs().filter((j) => j.project_id === st.project && j.status === "hecho" && j.changes && j.changes.files > 0);
          sourceSel = ui.select(jobs.length ? jobs.slice(0, 50).map((j) => ({ value: j.id, label: u.trunc(j.title || j.id, 60) + " · " + (j.agent.name || "") + " · +" + j.changes.additions + " −" + j.changes.deletions })) : [{ value: "", label: "No hay trabajos con cambios en este proyecto" }], query.get("desde") || "");
          ctl = ui.field("Trabajo con cambios", sourceSel, { field: "source", hint: "Su parche se aplica en el árbol antes de que el equipo empiece." });
        } else if (st.sourceType === "pr") {
          const prev = prInput ? prInput.value : (prQuery ? String(qPR) : "");
          prInput = ui.input({ type: "number", min: 1, inputmode: "numeric", placeholder: "Número del PR", value: prev });
          const onPR = () => {
            const n = Number(prInput.value);
            if (Number.isInteger(n) && n > 0) setAutoTask(NEUTRAL.pr(n));
          };
          prInput.addEventListener("input", onPR);
          const pick = O.sel.githubReady(st.project) ? ui.btn("Elegir…", {
            size: "sm", icon: "branch",
            onClick: () => {
              const d = O.dialog({ title: "Elegir un PR", wide: true, body: githubPicker(st.project, (type, x) => { prInput.value = String(x.number); onPR(); d.close(); }, "pr"), actions: [{ label: "Cerrar", kind: "ghost" }] });
            },
          }) : null;
          ctl = ui.field("Pull request", h("div", { class: "inline-row" }, prInput, pick), { field: "source", hint: O.sel.githubReady(st.project) ? "El título, la descripción y el diff del PR se pasan al equipo como datos externos no confiables." : "Necesita el token de GitHub de la Oficina (Ajustes)." });
        }
        O.put(sourceBox, ui.field(t.needs === "pr" ? "Fuente (PR)" : "Fuente", typeSeg.el), ctl);
      }

      const submit = ui.btn("Reunir al equipo", { type: "submit", kind: "primary", icon: "team", cls: "btn-lg" });
      O.put(form,
        h("div", { class: "tpl-detail-head" },
          h("span", { class: "tpl-icon big", "aria-hidden": "true", text: t.icon || "🧑‍🤝‍🧑" }),
          h("div", null, h("h2", { class: "section-title", text: t.name }), h("p", { class: "muted", text: t.description || "" }))),
        ui.field("Proyecto", projSel, { field: "project_id" }),
        h("fieldset", { class: "seats", dataset: { field: "participants" } },
          h("legend", { class: "field-label", text: "Asientos del equipo" }),
          h("div", { class: "seat-grid" }, seats.map(({ r, sel }) => ui.field(r.label, sel))),
          h("p", { class: "field-error", role: "alert" })),
        sourceBox,
        ui.field("Encargo del equipo", task, { field: "task", counter: ui.counter(task, Math.min((snap.limits.max_prompt_bytes || 32768), 32768)) }),
        h("div", { class: "grid-3" },
          t.iterative ? ui.field("Máximo de rondas", maxIt, { field: "max_iterations", hint: "Corrección → revisión, mientras el revisor pida cambios." }) : null,
          ui.field("Título", title, { field: "title", optional: true }),
          ui.field("Rama", branch, { field: "branch", optional: true })),
        ui.field("Prioridad", prio.el, { field: "priority" }),
        h("div", { class: "form-foot" },
          h("p", { class: "form-summary", text: "Cada paso es un trabajo normal: entra en la cola, corre en el sandbox y deja su resultado al siguiente." }), submit));
      drawSource();

      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        ui.fieldError(form, null);
        if (!task.value.trim()) return ui.fieldError(form, { field: "task", message: "Escribe el encargo del equipo." });
        if (!ui.valid.branch(branch.value.trim())) return ui.fieldError(form, { field: "branch", message: "Nombre de rama no válido." });
        const body = {
          template: t.id, project_id: st.project, task: task.value,
          participants: Object.fromEntries(seats.map(({ r, sel }) => [r.role, sel.value])),
          priority: st.priority,
        };
        if (t.iterative) body.max_iterations = Number(maxIt.value);
        if (title.value.trim()) body.title = title.value.trim();
        if (branch.value.trim()) body.branch = branch.value.trim();
        if (issueHere()) {
          body.source = { type: "issue", number: st.issue };
        } else if (st.sourceType === "job") {
          if (!sourceSel || !sourceSel.value) return ui.fieldError(form, { field: "source", message: "Elige un trabajo con cambios." });
          body.source = { type: "job", job_id: sourceSel.value };
        } else if (st.sourceType === "pr") {
          const n = Number(prInput && prInput.value);
          if (!Number.isInteger(n) || n < 1) return ui.fieldError(form, { field: "source", message: "Indica el número del PR." });
          body.source = { type: "pr", number: n };
        }
        await ui.busy(submit, async () => {
          try {
            const p = await O.api.post("/api/pipelines", body);
            lastProject = st.project;
            O.toast("Equipo reunido: los pasos entran en la cola.", { kind: "ok" });
            O.store.sync();
            O.go(p && p.id ? "#/equipos/" + O.enc(p.id) : "#/equipos");
          } catch (ex) {
            ui.fieldError(form, ex);
          }
        });
        return undefined;
      });
      O.put(detail, h("div", { class: "card tpl-detail" }, form));
    }

    drawTemplates();
    drawDetail();
    return h("div", null,
      h("p", { class: "muted lead", text: "Un equipo encadena varios agentes: cada uno hace un paso y le pasa el resultado (y los cambios) al siguiente. Mientras trabajan, los verás en la sala de reuniones." }),
      tplGrid, detail);
  }

  O.route("/nuevo", {
    title: "Nuevo",
    mount(root, params, query) {
      const tabs = ui.tabs({
        tabs: [
          { id: "trabajo", label: "🧑‍💻 Trabajo", render: () => jobForm(query) },
          { id: "equipo", label: "🧑‍🤝‍🧑 Equipo", render: () => teamForm(query) },
        ],
        active: query.get("tab") === "equipo" ? "equipo" : "trabajo",
      });
      O.put(root,
        ui.pageHead({ title: "Nuevo encargo", icon: "✨", subtitle: "Encarga un trabajo a un agente o reúne a un equipo. Todo corre en un sandbox aislado, de uno en uno." }),
        tabs.el);
      return {};
    },
  });
})();
