/*
 * Oficina de agentes · Ajustes (#/ajustes): office settings, queue pause,
 * credentials status (never values), limits, export and version.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, fmt, ui } = O;
  const S = O.state;

  const LESSONS = [
    { value: "proponer", label: "Proponer", hint: "Las lecciones de cada trabajo llegan a la Bandeja y tú decides cuáles entran en la memoria. Recomendado." },
    { value: "aprobar", label: "Aprobar solas", hint: "Entran directamente en la memoria del proyecto, sin revisión. No recomendado." },
    { value: "off", label: "Ignorar", hint: "La Oficina no guarda lecciones." },
  ];

  function settingsForm() {
    const st = S.snap.settings;
    const form = h("form", { class: "form", novalidate: true });
    const name = ui.input({ maxlength: 60, value: st.office_name || "" });
    const agents = O.sel.agents();
    const def = ui.select(agents.map((a) => ({ value: a.id, label: a.emoji + " " + a.name })), st.default_agent);
    const lessonsWarn = h("div", { class: "notice notice-warn lessons-warn", role: "note", hidden: true },
      h("strong", { text: "⚠️ Las lecciones entran en la memoria sin que nadie las revise. " }),
      "Cada lección aprobada se añade a todos los prompts posteriores de ese proyecto. Un agente manipulado (por ejemplo, por el texto de una issue, de un PR o del propio repositorio) podría dejar ahí instrucciones que seguirían los trabajos siguientes. Recomendado: «Proponer», para revisarlas en la Bandeja. ",
      h("button", { type: "button", class: "link-btn", on: { click: () => lessons.set("proponer", true) } }, "Usar «Proponer»"));
    const syncLessonsWarn = (v) => { lessonsWarn.hidden = v !== "aprobar"; };
    const lessons = ui.segmented({ label: "Lecciones", showHint: true, options: LESSONS, value: st.auto_lessons || "proponer", onChange: syncLessonsWarn });
    syncLessonsWarn(lessons.value());
    const maxLessons = ui.input({ type: "number", min: 0, max: 200, value: st.max_lessons_in_prompt === undefined ? 12 : st.max_lessons_in_prompt, inputmode: "numeric" });
    const projects = O.sel.projects();
    const retro = ui.select(projects.map((p) => ({ value: p.id, label: p.name })), st.retro_project);
    const maxIt = ui.input({ type: "number", min: 1, max: 10, value: st.max_iterations || 2, inputmode: "numeric" });
    const submit = ui.btn("Guardar ajustes", { type: "submit", kind: "primary", icon: "check" });
    O.put(form,
      h("div", { class: "grid-2" },
        ui.field("Nombre de la oficina", name, { field: "office_name" }),
        ui.field("Agente por defecto", def, { field: "default_agent", hint: "El que aparece elegido al encargar trabajo." })),
      ui.field("Lecciones aprendidas", lessons.el, { field: "auto_lessons" }),
      lessonsWarn,
      h("div", { class: "grid-3" },
        ui.field("Lecciones por encargo", maxLessons, { field: "max_lessons_in_prompt", hint: "Cuántas lecciones de la memoria se añaden a cada prompt." }),
        ui.field("Proyecto del Coach y del juez", retro, { field: "retro_project", hint: "Donde corren los trabajos que solo leen su prompt (retro, juez)." }),
        ui.field("Rondas máximas de un equipo", maxIt, { field: "max_iterations", hint: "Corrección → revisión, como mucho." })),
      h("div", { class: "form-foot" }, h("span"), submit));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ui.fieldError(form, null);
      if (!name.value.trim()) return ui.fieldError(form, { field: "office_name", message: "Ponle un nombre." });
      const ml = Number(maxLessons.value), mi = Number(maxIt.value);
      if (!Number.isInteger(ml) || ml < 0 || ml > 200) return ui.fieldError(form, { field: "max_lessons_in_prompt", message: "Entre 0 y 200." });
      if (!Number.isInteger(mi) || mi < 1 || mi > 10) return ui.fieldError(form, { field: "max_iterations", message: "Entre 1 y 10." });
      const body = {
        office_name: name.value.trim(), default_agent: def.value, auto_lessons: lessons.value(),
        max_lessons_in_prompt: ml, queue_paused: !!S.snap.settings.queue_paused, retro_project: retro.value, max_iterations: mi,
      };
      await ui.busy(submit, async () => {
        try {
          await O.api.post("/api/settings", body);
          Object.assign(S.snap.settings, body);
          O.toast("Ajustes guardados.", { kind: "ok" });
          O.store.sync();
          O.emit("store", { full: false, jobs: [] });
        } catch (ex) {
          ui.fieldError(form, ex);
        }
      });
      return undefined;
    });
    return form;
  }

  function credRow(label, ok, detail, how, extra) {
    return h("li", { class: ["cred-row", ok ? "is-ok" : "is-missing"] },
      h("span", { class: "cred-icon", "aria-hidden": "true", text: ok ? "✅" : "⛔" }),
      h("div", null,
        h("strong", { text: label + (ok ? ": configurado" : ": sin configurar") }),
        detail ? h("p", { class: "muted small" }, detail) : null,
        !ok && how ? h("p", { class: "small" }, how) : null,
        extra || null));
  }

  /** Deletes the Codex session the office saved; the host Secret is used next. */
  async function forgetCodex(btn) {
    const ok = await O.confirm({
      title: "¿Olvidar la sesión de Codex guardada?",
      text: [
        "La Oficina borra la sesión de Codex que guardó tras las últimas ejecuciones. La próxima ejecución de Codex usará el secreto del servidor.",
        "Si ese secreto está caducado, los encargos de Codex fallarán hasta que lo renueves en el servidor con sudo ./scripts/ax-web-bootstrap.sh office --replace.",
      ],
      confirmLabel: "Olvidar la sesión",
      danger: true,
    });
    if (!ok) return;
    await ui.busy(btn, async () => {
      await O.api.post("/api/credentials/codex/forget", {});
      O.toast("Sesión de Codex olvidada: la próxima ejecución usará el secreto del servidor.", { kind: "ok" });
      // The credentials badge changes: one snapshot reload (debounced).
      O.store.refresh();
    }, "No se pudo olvidar la sesión de Codex");
  }

  function credentials() {
    const c = S.snap.credentials || {};
    const bootstrap = h("code", { text: "sudo ./scripts/ax-web-bootstrap.sh office" });
    return h("div", null,
      h("ul", { class: "cred-list" },
        credRow("Claude Code", c.claude, "Token OAuth de Claude Code: lo usan todos los agentes de Claude.", "Se configura con el despliegue del panel (bootstrap k8s)."),
        credRow("Codex", c.codex,
          c.codex ? ["Origen: ", c.codex_source === "guardada" ? "guardada por la Oficina (renovada tras una ejecución)" : c.codex_source === "secreto" ? "secreto del servidor" : c.codex_source || "—",
            c.codex_refreshed ? [" · última renovación: ", ui.time(c.codex_refreshed, { absolute: true })] : null] : "Sin él, los encargos a agentes de Codex fallan al empezar.",
          h("span", null, "Ejecuta en el servidor ", bootstrap, " (o ", h("code", { text: "office --replace" }), " si ya existe)."),
          c.codex_source === "guardada" ? h("div", { class: "cred-actions" },
            ui.btn("Olvidar la sesión de Codex guardada", { size: "sm", kind: "danger-ghost", icon: "trash", onClick: (e) => forgetCodex(e.currentTarget) }),
            h("p", { class: "muted small", text: "Úsalo si la sesión guardada dejó de funcionar o si has renovado el secreto del servidor." })) : null),
        credRow("GitHub", c.github,
          c.github ? "Propietarios: " + O.ownersText(c.github_owners) + ". Se usa para issues, PRs y comentarios; nunca entra en un sandbox." : "Sin él no hay issues, PRs ni comentarios.",
          h("span", null, "Guarda el token en /etc/dockerswarm/ax/github-token y ejecuta ", bootstrap, "."))),
      h("p", { class: "muted small", text: "El panel nunca muestra ni devuelve los valores de las credenciales: solo si existen." }));
  }

  function limits() {
    const l = S.snap.limits || {};
    const cat = S.snap.catalog || {};
    const row = (k, v) => h("div", { class: "meta-item" }, h("dt", { text: k }), h("dd", null, v));
    return h("dl", { class: "meta-grid" },
      row("Turnos máximos", fmt.num(l.max_turns)),
      row("Tiempo máximo", (l.max_timeout_minutes || "—") + " min"),
      row("Encargo máximo", fmt.bytes(l.max_prompt_bytes)),
      row("System prompt máximo", fmt.bytes(l.max_system_prompt_bytes)),
      row("Cola máxima", fmt.num(l.max_queue)),
      row("Hosts de repositorio", (l.repo_hosts || []).join(", ") || "—"),
      row("Claude Code", h("span", { class: "mono" }, (cat.claude && cat.claude.version) || "—")),
      row("Codex", h("span", { class: "mono" }, (cat.codex && cat.codex.version) || "—")),
      row("Versión de la Oficina", h("span", { class: "mono" }, S.snap.version || "—")));
  }

  O.route("/ajustes", {
    title: "Ajustes",
    mount(root) {
      const queueBox = h("div");
      const credBox = h("div");
      const limBox = h("div");
      const warnBox = h("div");
      O.put(root,
        ui.pageHead({ title: "Ajustes", icon: "⚙️", subtitle: "Preferencias de la oficina, credenciales y copia de seguridad." }),
        warnBox,
        ui.section("Oficina", { icon: "🏢" }, h("div", { class: "card" }, settingsForm())),
        ui.section("Cola", { icon: "🎫" }, queueBox),
        ui.section("Credenciales", { icon: "🔑" }, h("div", { class: "card" }, credBox)),
        ui.section("Copia de seguridad", { icon: "💾" }, h("div", { class: "card" },
          h("p", { text: "Descarga un JSON con todo el estado de la Oficina (agentes, proyectos, memoria, trabajos, equipos, turnos, propuestas y evaluaciones). No incluye credenciales." }),
          ui.btn("Exportar", { href: "/api/export", download: "oficina.json", icon: "download", kind: "primary" }))),
        ui.section("Límites y versiones", { icon: "📏" }, h("div", { class: "card" }, limBox)),
        ui.section("Atajos de teclado", { icon: "⌨️" }, h("p", { class: "muted" }, "Pulsa ", h("kbd", { text: "?" }), " en cualquier pantalla para verlos.")));
      const render = () => {
        const paused = !!S.snap.settings.queue_paused;
        ui.memo(queueBox, String(paused), () => h("div", { class: ["card", "queue-card", paused && "is-paused"] },
          h("p", null, h("strong", { text: paused ? "⏸ La cola está en pausa." : "▶ La cola está en marcha." }), " ",
            paused ? "Los trabajos se acumulan pero no arranca ninguno nuevo; el que esté en curso termina." : "En cuanto el sandbox queda libre arranca el siguiente trabajo (por prioridad y antigüedad)."),
          ui.btn(paused ? "Reanudar la cola" : "Pausar la cola", {
            kind: paused ? "primary" : null, icon: paused ? "play" : "pause",
            onClick: async (e) => {
              await ui.busy(e.currentTarget, async () => {
                await O.api.post("/api/queue/pause", { paused: !paused });
                S.snap.settings.queue_paused = !paused;
                O.toast(paused ? "Cola reanudada." : "Cola en pausa.", { kind: "ok" });
                O.emit("store", { full: false, jobs: [] });
              }, "No se pudo cambiar la cola");
            },
          })));
        ui.memo(credBox, JSON.stringify(S.snap.credentials), credentials);
        ui.memo(limBox, JSON.stringify([S.snap.limits, S.snap.version]), limits);
        ui.memo(warnBox, JSON.stringify(S.snap.warnings), () => S.snap.warnings.length
          ? h("div", { class: "notice notice-warn" }, h("strong", { text: "Avisos del servidor:" }), h("ul", null, S.snap.warnings.map((w) => h("li", { text: w }))))
          : null);
      };
      render();
      return { update: render };
    },
  });
})();
