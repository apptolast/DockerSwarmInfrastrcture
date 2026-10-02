/*
 * Oficina de agentes · Turnos (#/turnos): schedules that fire a job or a
 * team on UTC weekdays and time.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui, L } = O;
  const S = O.state;

  /** "HH:MM" UTC → local time hint for today. */
  function localHint(hhmm) {
    const m = /^(\d{2}):(\d{2})$/.exec(hhmm || "");
    if (!m) return "";
    const now = new Date(O.now());
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), Number(m[1]), Number(m[2])));
    const local = fmt.time(d);
    // Local calendar day vs UTC calendar day of the same instant.
    const diff = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) - Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    const shift = diff > 0 ? " del día siguiente" : diff < 0 ? " del día anterior" : "";
    return local + " hora local" + shift;
  }
  O.localHint = localHint;

  function daysText(days) {
    const d = (days || []).slice().sort((a, b) => ((a + 6) % 7) - ((b + 6) % 7));
    if (d.length === 7) return "todos los días";
    if (d.join(",") === "1,2,3,4,5") return "de lunes a viernes";
    if (d.length === 2 && d.includes(0) && d.includes(6)) return "fines de semana";
    return d.map((x) => String(O.own(L.days, x) || x).toLowerCase()).join(", ");
  }

  function targetText(t) {
    const p = O.sel.projectName(t.project_id);
    if (t.type === "pipeline") {
      const tpl = O.sel.template(t.template);
      return "Equipo " + (tpl ? tpl.icon + " " + tpl.name : t.template) + " · " + p;
    }
    const a = O.sel.agent(t.agent_id);
    return (a ? a.emoji + " " + a.name : t.agent_id) + " · " + ((O.own(L.kinds, t.kind) || {}).label || t.kind) + " · " + p;
  }

  function refLink(ref) {
    if (!ref) return null;
    if (O.sel.job(ref)) return h("a", { href: "#/trabajo/" + O.enc(ref), class: "mono", text: ref });
    if (O.sel.pipeline(ref)) return h("a", { href: "#/equipos/" + O.enc(ref), class: "mono", text: ref });
    return h("span", { class: "mono", text: ref });
  }

  function editor(sc, onDone) {
    const s0 = sc || { name: "", enabled: true, days: [1, 2, 3, 4, 5], time: "07:00", target: { type: "job", project_id: S.snap.settings.retro_project, agent_id: S.snap.settings.default_agent, kind: "pregunta", template: "", prompt: "", branch: "" } };
    const t0 = s0.target || {};
    const form = h("form", { class: "form", novalidate: true });
    const name = ui.input({ maxlength: 60, value: s0.name, placeholder: "p. ej. Auditoría semanal" });
    const enabled = h("input", { type: "checkbox", class: "switch", checked: s0.enabled });
    const days = ui.dayChips(s0.days);
    const time = ui.input({ type: "time", value: s0.time || "07:00", step: 60, class: "input mono" });
    const hint = h("span", { class: "field-hint" });
    const syncHint = () => { hint.textContent = "= " + localHint(time.value); };
    time.addEventListener("input", syncHint);
    syncHint();
    let type = t0.type === "pipeline" ? "pipeline" : "job";
    const typeSeg = ui.segmented({ label: "Qué lanza", options: [{ value: "job", label: "🧑‍💻 Un trabajo" }, { value: "pipeline", label: "🧑‍🤝‍🧑 Un equipo" }], value: type, onChange: (v) => { type = v; syncType(); } });
    const projects = O.sel.projects().filter((p) => !p.archived || p.id === t0.project_id);
    const proj = ui.select(projects.map((p) => ({ value: p.id, label: p.name })), t0.project_id || (projects[0] && projects[0].id));
    const agents = O.sel.agents().filter((a) => a.enabled || a.id === t0.agent_id);
    const agent = ui.select(agents.map((a) => ({ value: a.id, label: a.emoji + " " + a.name + " — " + a.role })), t0.agent_id || S.snap.settings.default_agent);
    const kind = ui.select(["pregunta", "plan", "cambio", "revision"].map((k) => ({ value: k, label: L.kinds[k].icon + " " + L.kinds[k].label })), t0.kind || "pregunta");
    const tpls = S.snap.templates.filter((t) => t.id !== "evaluacion");
    const tpl = ui.select(tpls.map((t) => ({ value: t.id, label: (t.icon || "") + " " + t.name })), t0.template || (tpls[0] && tpls[0].id));
    const prompt = ui.textarea({ rows: 5, placeholder: "El encargo que se lanzará cada vez." });
    prompt.value = t0.prompt || "";
    const branch = ui.input({ maxlength: 200, class: "input mono", value: t0.branch || "", placeholder: "rama del proyecto" });
    const jobFields = h("div", { class: "grid-2" }, ui.field("Agente", agent, { field: "target.agent_id" }), ui.field("Tipo", kind, { field: "target.kind" }));
    const pipeFields = ui.field("Equipo", tpl, { field: "target.template" });
    function syncType() {
      jobFields.hidden = type !== "job";
      pipeFields.hidden = type !== "pipeline";
    }
    syncType();
    const submit = ui.btn(sc ? "Guardar turno" : "Crear turno", { type: "submit", kind: "primary", icon: "check" });
    O.put(form,
      h("div", { class: "grid-2" }, ui.field("Nombre", name, { field: "name" }),
        h("label", { class: "switch-row" }, enabled, h("span", { text: "Activo" }))),
      h("div", { class: "grid-2" },
        h("div", { class: "field", dataset: { field: "days" } }, h("span", { class: "field-label", text: "Días (UTC)" }), days.el, h("p", { class: "field-error", role: "alert" })),
        h("div", { class: "field", dataset: { field: "time" } }, h("label", { class: "field-label", for: time.id || (time.id = u.uid("f")), text: "Hora (UTC)" }), time, hint, h("p", { class: "field-error", role: "alert" }))),
      ui.field("Qué lanza", typeSeg.el),
      ui.field("Proyecto", proj, { field: "target.project_id" }),
      jobFields, pipeFields,
      ui.field("Encargo", prompt, { field: "target.prompt", counter: ui.counter(prompt, 32768) }),
      ui.field("Rama", branch, { field: "target.branch", optional: true }),
      h("div", { class: "form-foot" }, h("p", { class: "muted small", text: "Los turnos van en UTC (como el servidor). Si el anterior aún está en cola o en curso, no se lanza otro." }), h("div", { class: "row-actions" },
        onDone ? ui.btn("Cancelar", { kind: "ghost", onClick: () => onDone(null) }) : null, submit)));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ui.fieldError(form, null);
      const err = (field, message) => ui.fieldError(form, { field, message });
      if (!name.value.trim()) return err("name", "Ponle un nombre.");
      if (u.runes(name.value) > 60) return err("name", "Máximo 60 caracteres.");
      if (!days.value().length) return err("days", "Elige al menos un día.");
      if (!/^\d{2}:\d{2}$/.test(time.value)) return err("time", "Hora no válida.");
      if (!prompt.value.trim()) return err("target.prompt", "Escribe el encargo.");
      if (!ui.valid.branch(branch.value.trim())) return err("target.branch", "Rama no válida.");
      const target = { type, project_id: proj.value, prompt: prompt.value };
      if (type === "job") {
        target.agent_id = agent.value;
        target.kind = kind.value;
      } else target.template = tpl.value;
      if (branch.value.trim()) target.branch = branch.value.trim();
      const body = { name: name.value.trim(), enabled: enabled.checked, days: days.value(), time: time.value, target };
      if (sc) body.id = sc.id;
      await ui.busy(submit, async () => {
        try {
          await O.api.post("/api/schedules", body);
          O.toast(sc ? "Turno guardado." : "Turno creado.", { kind: "ok" });
          O.store.sync();
          if (onDone) onDone(true);
        } catch (ex) {
          ui.fieldError(form, ex);
        }
      });
      return undefined;
    });
    return form;
  }

  function card(sc, state) {
    if (state.editing === sc.id) {
      return h("article", { class: "card" }, editor(sc, () => { state.editing = null; state.redraw(); }));
    }
    const t = sc.target || {};
    return h("article", { class: ["card", "schedule", !sc.enabled && "is-off"] },
      h("header", { class: "card-head" },
        h("span", { class: "card-icon", "aria-hidden": "true", text: "⏰" }),
        h("div", { class: "card-head-text" },
          h("h3", { class: "card-title", text: sc.name }),
          h("p", { class: "card-sub" }, h("strong", { text: (sc.time || "--:--") + " UTC" }), h("span", { class: "muted", text: " (" + localHint(sc.time) + ") · " + daysText(sc.days) }))),
        sc.enabled ? h("span", { class: "badge badge-ok", text: "activo" }) : h("span", { class: "badge badge-soft", text: "pausado" })),
      h("p", { class: "schedule-target", text: targetText(t) }),
      t.prompt ? h("p", { class: "muted small", text: u.trunc(t.prompt, 200) }) : null,
      h("p", { class: "small" },
        sc.enabled && sc.next_run ? ["Próximo: ", h("strong", { title: fmt.utcDateTime(sc.next_run), text: fmt.dateTime(sc.next_run) }), " (", ui.time(sc.next_run), ")"] : "Sin próxima ejecución",
        sc.last_run ? [" · último: ", ui.time(sc.last_run)] : null,
        sc.last_ref ? [" → ", refLink(sc.last_ref)] : null),
      h("div", { class: "row-actions" },
        ui.btn("Ejecutar ahora", {
          size: "sm", icon: "play", kind: "primary",
          onClick: async (e) => {
            await ui.busy(e.currentTarget, async () => {
              await O.api.post("/api/schedules/" + O.enc(sc.id) + "/run", {});
              O.toast("Lanzado: «" + sc.name + "» está en la cola.", { kind: "ok" });
              O.store.sync();
            }, "No se pudo lanzar");
          },
        }),
        ui.btn("Editar", { size: "sm", icon: "edit", onClick: () => { state.editing = sc.id; state.redraw(); } }),
        h("span", { class: "spacer" }),
        ui.btn("Borrar", {
          size: "sm", icon: "trash", kind: "danger-ghost",
          onClick: async (e) => {
            const btn = e.currentTarget;
            if (!(await O.confirm({ title: "¿Borrar el turno «" + sc.name + "»?", text: "Los trabajos que ya lanzó se conservan.", confirmLabel: "Borrar", danger: true }))) return;
            await ui.busy(btn, async () => {
              await O.api.post("/api/schedules/" + O.enc(sc.id) + "/delete", {});
              O.toast("Turno borrado.", { kind: "ok" });
              O.store.sync();
            }, "No se pudo borrar");
          },
        })));
  }

  O.route("/turnos", {
    title: "Turnos",
    mount(root) {
      const body = h("div");
      const state = { editing: null, creating: false, redraw: () => { body._sig = null; render(); } };
      O.put(root, ui.pageHead({
        title: "Turnos", icon: "⏰",
        subtitle: "Encargos que se repiten solos: una auditoría cada lunes, una revisión de dependencias cada noche… Las horas son UTC, como el servidor.",
        actions: ui.btn("Nuevo turno", { kind: "primary", icon: "plus", onClick: () => { state.creating = true; state.redraw(); } }),
      }), body);
      const render = () => {
        const list = S.snap.schedules;
        ui.memo(body, JSON.stringify([list, state.editing, state.creating]), () => [
          state.creating ? h("article", { class: "card" }, h("h2", { class: "section-title", text: "Nuevo turno" }), editor(null, () => { state.creating = false; state.redraw(); })) : null,
          list.length ? h("div", { class: "card-list" }, list.map((sc) => card(sc, state)))
            : state.creating ? null : ui.empty({
              icon: "⏰", title: "No hay turnos",
              text: "Un turno lanza un trabajo o un equipo en los días y a la hora que elijas (UTC). Si el anterior sigue en marcha, espera a la siguiente vez.",
              actions: ui.btn("Crear el primero", { kind: "primary", icon: "plus", onClick: () => { state.creating = true; state.redraw(); } }),
            }),
        ]);
      };
      render();
      return { update: render };
    },
  });
})();
