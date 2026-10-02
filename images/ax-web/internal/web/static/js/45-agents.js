/*
 * Oficina de agentes · Agentes (#/agentes, #/agentes/<id>, #/agentes/nuevo):
 * the team's personas, their editor, version history with prompt diffs and
 * rollback, per-version metrics and the Coach.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui, L } = O;
  const S = O.state;

  const EMOJIS = ["🛠️", "🏛️", "🔍", "🧪", "🛡️", "📚", "🤖", "⚡", "🧭", "🎨", "🚀", "🐛", "🧠", "📈", "🦊", "🐙", "🦉", "🧹", "🔧", "🧙"];
  const COLORS = ["#8b5cf6", "#22c55e", "#f59e0b", "#06b6d4", "#ef4444", "#ec4899", "#3b82f6", "#a3a3a3", "#14b8a6", "#f97316", "#84cc16", "#6366f1"];

  function metricsOf(agentId) {
    return (S.snap.metrics.by_agent_version || []).filter((m) => m.agent_id === agentId).sort((a, b) => b.version - a.version);
  }

  function agentTotals(agentId) {
    const ms = metricsOf(agentId);
    const t = { jobs: 0, ok: 0, up: 0, down: 0 };
    for (const m of ms) {
      t.jobs += m.jobs;
      t.ok += m.succeeded;
      t.up += m.thumbs_up;
      t.down += m.thumbs_down;
    }
    return t;
  }

  // ---------------------------------------------------------------- list --

  function agentCard(a) {
    const st = O.sel.agentState(a.id);
    const t = agentTotals(a.id);
    const toggle = h("input", {
      type: "checkbox", class: "switch", checked: a.enabled, "aria-label": (a.enabled ? "Desactivar a " : "Activar a ") + a.name,
      on: {
        change: async (e) => {
          const el = e.currentTarget;
          el.disabled = true;
          try {
            await saveAgent(a, Object.assign(editable(a), { enabled: el.checked }));
            O.toast(a.name + (el.checked ? " vuelve a la oficina." : " se toma un descanso (desactivado)."), { kind: "ok" });
          } catch (err) {
            el.checked = !el.checked;
            O.fail(err, "No se pudo cambiar");
          } finally {
            el.disabled = false;
          }
        },
      },
    });
    return h("article", { class: ["card", "agent-tile", !a.enabled && "is-off"], style: { "--agent": u.color(a.color) } },
      h("div", { class: "agent-tile-top" },
        ui.avatar(a, "lg"),
        h("div", { class: "agent-tile-text" },
          h("h3", { class: "card-title" }, h("a", { href: "#/agentes/" + O.enc(a.id), text: a.name })),
          h("p", { class: "muted", text: a.role })),
        h("label", { class: "switch-wrap", title: a.enabled ? "Activo" : "Desactivado" }, toggle)),
      h("p", { class: "mono small", text: (L.harness[a.harness] || a.harness) + " · " + ui.modelLabel(a.harness, a.model, a.effort) }),
      h("div", { class: "chips" },
        h("span", { class: ["badge", a.mode === "completo" ? "badge-full" : "badge-soft"], title: (L.modes[a.mode] || {}).hint || null, text: (L.modes[a.mode] || {}).label || a.mode }),
        h("span", { class: "badge badge-soft", text: "v" + a.version }),
        st.state === "working" ? h("span", { class: "badge badge-live", text: "trabajando" }) : null,
        st.queued.length ? h("span", { class: "badge badge-soft", text: st.queued.length + " en cola" }) : null),
      h("p", { class: "agent-tile-stats muted" },
        t.jobs ? fmt.num(t.jobs) + " trabajos · " + fmt.pct(t.ok / t.jobs) + " éxito · 👍 " + t.up + " · 👎 " + t.down : "Sin trabajos terminados todavía"),
      h("div", { class: "row-actions" },
        ui.btn("Encargar", { href: "#/nuevo?agente=" + O.enc(a.id), size: "sm", icon: "plus" }),
        ui.btn("Editar", { href: "#/agentes/" + O.enc(a.id), size: "sm", kind: "ghost", icon: "edit" })));
  }

  O.route("/agentes", {
    title: "Agentes",
    mount(root) {
      const body = h("div");
      O.put(root, ui.pageHead({
        title: "Agentes", icon: "🧑‍💼",
        subtitle: "Cada agente es una persona del equipo: un motor (Claude Code o Codex), un modelo, cuánto razona, qué puede hacer y quién es (su system prompt). Cada cambio de comportamiento crea una versión nueva para poder compararlas.",
        actions: ui.btn("Nuevo agente", { href: "#/agentes/nuevo", kind: "primary", icon: "plus" }),
      }), body);
      const render = () => ui.memo(body, JSON.stringify([S.snap.agents, S.snap.metrics.by_agent_version, O.sel.agents().map((a) => O.sel.agentState(a.id).state)]), () =>
        S.snap.agents.length ? h("div", { class: "tile-grid" }, S.snap.agents.map(agentCard))
          : ui.empty({ icon: "🪑", title: "No hay agentes", text: "Crea el primero.", actions: ui.btn("Nuevo agente", { href: "#/agentes/nuevo", kind: "primary" }) }));
      render();
      return { update: render };
    },
  });

  // -------------------------------------------------------------- editor --

  function editable(a) {
    return {
      name: a.name, role: a.role, emoji: a.emoji, color: a.color, harness: a.harness, model: a.model || "",
      fallback_model: a.fallback_model || "", effort: a.effort || "", mode: a.mode, max_turns: a.max_turns,
      timeout_minutes: a.timeout_minutes, system_prompt: a.system_prompt || "", disallowed_tools: a.disallowed_tools || [],
      enabled: a.enabled,
    };
  }

  async function saveAgent(a, body) {
    const r = await O.api.post(a ? "/api/agents/" + O.enc(a.id) : "/api/agents", body);
    O.store.sync();
    return r;
  }

  function editorForm(agent, onSaved) {
    const lim = S.snap.limits || {};
    const maxSys = lim.max_system_prompt_bytes || 16384;
    const base = agent ? editable(agent) : {
      name: "", role: "", emoji: "🤖", color: COLORS[Math.floor(Math.random() * COLORS.length)], harness: "claude", model: "sonnet",
      fallback_model: "", effort: "medium", mode: "lectura", max_turns: 40, timeout_minutes: 30, system_prompt: "", disallowed_tools: [], enabled: true,
    };
    const form = h("form", { class: "form", novalidate: true });
    const preview = ui.avatar(base, "xl");
    const idInput = agent ? null : ui.input({ maxlength: 32, placeholder: "se genera del nombre", class: "input mono", autocomplete: "off", spellcheck: "false" });
    const name = ui.input({ maxlength: 60, value: base.name, required: true });
    const role = ui.input({ maxlength: 60, value: base.role, placeholder: "p. ej. Revisora de código" });
    const emoji = ui.input({ maxlength: 16, value: base.emoji, class: "input emoji-input", "aria-label": "Emoji" });
    const color = ui.input({ type: "color", value: u.color(base.color), class: "input color-input", "aria-label": "Color" });
    const palette = h("div", { class: "chips emoji-palette" }, EMOJIS.map((e) => h("button", {
      type: "button", class: "chip chip-btn emoji-btn", "aria-label": "Usar " + e, on: { click: () => { emoji.value = e; emoji.dispatchEvent(new Event("input", { bubbles: true })); } },
    }, e)));
    const swatches = h("div", { class: "chips" }, COLORS.map((c) => h("button", {
      type: "button", class: "swatch", style: { "--swatch": c }, "aria-label": "Color " + c, title: c,
      on: { click: () => { color.value = c; color.dispatchEvent(new Event("input", { bubbles: true })); } },
    })));
    const syncPreview = () => {
      preview.textContent = emoji.value || "🤖";
      preview.style.setProperty("--agent", u.color(color.value));
    };
    emoji.addEventListener("input", syncPreview);
    color.addEventListener("input", syncPreview);
    const picker = ui.modelPicker({ harness: base.harness, model: base.model, effort: base.effort, fallback_model: base.fallback_model }, { onChange: () => syncHarness() });
    const modeSeg = ui.segmented({
      label: "Modo", showHint: true,
      options: ["lectura", "completo"].map((m) => ({ value: m, label: L.modes[m].label, hint: L.modes[m].hint })),
      value: base.mode, onChange: () => form.dispatchEvent(new Event("input")),
    });
    const turns = ui.input({ type: "number", min: 1, max: lim.max_turns || 500, value: base.max_turns || 40, inputmode: "numeric" });
    const timeout = ui.input({ type: "number", min: 5, max: lim.max_timeout_minutes || 180, value: base.timeout_minutes || 30, inputmode: "numeric" });
    const sys = ui.textarea({ rows: 16, class: "input textarea mono", value: base.system_prompt, placeholder: "Quién es, cómo trabaja, qué prioriza, qué evita… En español." });
    sys.value = base.system_prompt;
    const tools = ui.textarea({ rows: 3, class: "input textarea mono", placeholder: "Una por línea, p. ej. WebFetch" });
    tools.value = (base.disallowed_tools || []).join("\n");
    const enabled = h("input", { type: "checkbox", class: "switch", checked: base.enabled });
    const turnsField = ui.field("Turnos máximos", turns, { field: "max_turns", hint: "1-" + (lim.max_turns || 500) + ". Cada turno es una respuesta del modelo con sus herramientas." });
    const toolsField = ui.field("Herramientas prohibidas", tools, { field: "disallowed_tools", optional: true, hint: "Solo Claude en modo completo. Máximo 20." });
    function syncHarness() {
      const isClaude = picker.value().harness === "claude";
      turnsField.hidden = !isClaude;
      toolsField.hidden = !isClaude;
    }
    syncHarness();

    const submit = ui.btn(agent ? "Guardar cambios" : "Crear agente", { type: "submit", kind: "primary", icon: "check", cls: "btn-lg" });
    O.put(form,
      h("div", { class: "agent-identity" },
        preview,
        h("div", { class: "grid-2 grow" },
          ui.field("Nombre", name, { field: "name" }),
          ui.field("Rol", role, { field: "role" }),
          idInput ? ui.field("Identificador", idInput, { field: "id", optional: true, hint: "Minúsculas, números y guiones; no se puede cambiar después." }) : null)),
      h("div", { class: "grid-2" },
        h("div", { class: "field", dataset: { field: "emoji" } }, h("label", { class: "field-label", for: emoji.id || (emoji.id = u.uid("f")), text: "Emoji" }), emoji, palette, h("p", { class: "field-error", role: "alert" })),
        h("div", { class: "field", dataset: { field: "color" } }, h("label", { class: "field-label", for: color.id || (color.id = u.uid("f")), text: "Color" }), h("div", { class: "inline-row" }, color, swatches), h("p", { class: "field-error", role: "alert" }))),
      h("h3", { class: "form-h", text: "Cerebro" }),
      picker.el,
      ui.field("Modo", modeSeg.el, { field: "mode" }),
      h("div", { class: "grid-2" }, turnsField,
        ui.field("Tiempo máximo (min)", timeout, { field: "timeout_minutes", hint: "5-" + (lim.max_timeout_minutes || 180) + " minutos por trabajo." })),
      h("h3", { class: "form-h", text: "Personalidad" }),
      ui.field("System prompt (persona)", sys, { field: "system_prompt", counter: ui.counter(sys, maxSys), hint: "Se añade al prompt de sistema de Claude (o al principio del encargo en Codex). El Coach puede proponer mejoras basadas en sus resultados." }),
      toolsField,
      h("label", { class: "switch-row" }, enabled, h("span", null, h("strong", { text: "Activo" }), h("span", { class: "muted", text: " — los desactivados no aparecen en la oficina ni reciben encargos." }))),
      agent ? h("p", { class: "field-hint", text: "Cambiar motor, modelo, esfuerzo, modo o system prompt crea la versión v" + (agent.version + 1) + "; la actual queda en el historial." }) : null,
      h("div", { class: "form-foot" }, h("span"), submit));

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ui.fieldError(form, null);
      const err = (field, message) => { ui.fieldError(form, { field, message }); };
      const mp = picker.value();
      const toolList = tools.value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
      if (!name.value.trim()) return err("name", "Ponle un nombre.");
      if (u.runes(name.value) > 60) return err("name", "Máximo 60 caracteres.");
      if (u.runes(role.value) > 60) return err("role", "Máximo 60 caracteres.");
      if (idInput && idInput.value && !ui.valid.id(idInput.value)) return err("id", "Empieza por letra; 2-32 caracteres a-z, 0-9 y guiones.");
      if (u.bytes(emoji.value) > 16) return err("emoji", "Demasiado largo (máximo 16 bytes).");
      if (!ui.valid.color(color.value)) return err("color", "Color no válido.");
      const pv = picker.validate();
      if (pv) return err(pv.field, pv.message);
      const tn = Number(turns.value), tm = Number(timeout.value);
      if (mp.harness === "claude" && !(Number.isInteger(tn) && tn >= 1 && tn <= (lim.max_turns || 500))) return err("max_turns", "Entre 1 y " + (lim.max_turns || 500) + ".");
      if (!(Number.isInteger(tm) && tm >= 5 && tm <= (lim.max_timeout_minutes || 180))) return err("timeout_minutes", "Entre 5 y " + (lim.max_timeout_minutes || 180) + ".");
      if (u.bytes(sys.value) > maxSys) return err("system_prompt", "Supera " + fmt.bytes(maxSys) + ".");
      if (toolList.length > 20) return err("disallowed_tools", "Máximo 20 herramientas.");
      const badTool = toolList.find((t) => !ui.valid.tool(t));
      if (badTool) return err("disallowed_tools", "Nombre de herramienta no válido: " + badTool);
      const body = {
        name: name.value.trim(), role: role.value.trim(), emoji: emoji.value.trim() || "🤖", color: color.value,
        harness: mp.harness, model: mp.model, fallback_model: mp.fallback_model, effort: mp.effort,
        mode: modeSeg.value(), max_turns: mp.harness === "claude" ? tn : 0, timeout_minutes: tm,
        system_prompt: sys.value, disallowed_tools: mp.harness === "claude" ? toolList : [], enabled: enabled.checked,
      };
      if (idInput && idInput.value) body.id = idInput.value;
      await ui.busy(submit, async () => {
        try {
          const r = await saveAgent(agent, body);
          O.toast(agent ? "Guardado." + (r && r.version && r.version !== agent.version ? " Ahora es la versión v" + r.version + "." : "") : "Agente creado: ya tiene escritorio en la oficina.", { kind: "ok" });
          if (onSaved) onSaved(r);
        } catch (ex) {
          ui.fieldError(form, ex);
        }
      });
      return undefined;
    });
    return form;
  }

  function historyPanel(agent) {
    const versions = [{ version: agent.version, harness: agent.harness, model: agent.model, effort: agent.effort, mode: agent.mode, system_prompt: agent.system_prompt, note: "versión actual", at: agent.updated, current: true }]
      .concat((agent.history || []).slice().sort((a, b) => b.version - a.version));
    const diffBox = h("div", { class: "history-diff" });
    let selected = null;
    const list = h("ol", { class: "history-list" });
    const metrics = new Map(metricsOf(agent.id).map((m) => [m.version, m]));
    function draw() {
      O.put(list, versions.map((v) => {
        const m = metrics.get(v.version);
        return h("li", { class: ["history-item", selected === v.version && "is-on"] },
          h("button", {
            type: "button", class: "history-btn", disabled: v.current || null,
            "aria-pressed": selected === v.version ? "true" : "false",
            on: { click: () => { selected = v.version; draw(); showDiff(v); } },
          },
          h("strong", { text: "v" + v.version }),
          h("span", { class: "muted mono small", text: ui.modelLabel(v.harness, v.model, v.effort) + " · " + v.mode }),
          h("span", { class: "small", text: v.note || "" }),
          h("span", { class: "muted small" }, ui.time(v.at)),
          m && m.jobs ? h("span", { class: "small", text: m.jobs + " trab. · " + fmt.pct(m.succeeded / m.jobs) + " éxito" + (m.eval_runs ? " · nota " + fmt.num1(m.eval_score_avg) : "") }) : null));
      }));
    }
    function showDiff(v) {
      const changes = [];
      for (const k of ["harness", "model", "effort", "mode"]) {
        if ((v[k] || "") !== (agent[k] || "")) changes.push(h("li", null, h("strong", { text: k + ": " }), h("del", { text: v[k] || "(predet.)" }), " → ", h("ins", { text: agent[k] || "(predet.)" })));
      }
      O.put(diffBox,
        h("div", { class: "history-diff-head" },
          h("h4", { text: "v" + v.version + " → v" + agent.version + " (actual)" }),
          ui.btn("Restaurar v" + v.version, {
            size: "sm", icon: "undo",
            onClick: async (e) => {
              const btn = e.currentTarget;
              const ok = await O.confirm({ title: "¿Restaurar la v" + v.version + "?", text: "Se crea una versión nueva (v" + (agent.version + 1) + ") con la configuración y el prompt de la v" + v.version + ". Nada se pierde: la actual queda en el historial.", confirmLabel: "Restaurar" });
              if (!ok) return;
              await ui.busy(btn, async () => {
                await O.api.post("/api/agents/" + O.enc(agent.id) + "/rollback", { version: v.version });
                O.toast("Restaurada la v" + v.version + " como versión nueva.", { kind: "ok" });
                O.store.sync();
              }, "No se pudo restaurar");
            },
          })),
        changes.length ? h("ul", { class: "field-changes" }, changes) : null,
        h("p", { class: "muted small", text: "Diferencias del system prompt (− v" + v.version + ", + actual):" }),
        O.diff.renderLines(O.diff.lines(v.system_prompt || "", agent.system_prompt || "")));
    }
    draw();
    return h("div", null,
      versions.length > 1 ? h("p", { class: "muted small", text: "Elige una versión anterior para ver qué cambió y restaurarla." }) : h("p", { class: "muted small", text: "Aún no hay versiones anteriores: cada cambio de comportamiento guardará la actual aquí (hasta 20)." }),
      list, diffBox);
  }

  function metricsTable(agent) {
    const ms = metricsOf(agent.id);
    if (!ms.length) return h("p", { class: "muted", text: "Sin métricas todavía: aparecen cuando la versión termina trabajos." });
    const row = (m) => h("tr", null,
      h("td", { text: "v" + m.version }),
      h("td", { class: "num", text: fmt.num(m.jobs) }),
      h("td", { class: "num", text: m.jobs ? fmt.pct(m.succeeded / m.jobs) : "—" }),
      h("td", { class: "num", text: m.thumbs_up + " / " + m.thumbs_down }),
      h("td", { class: "num", text: m.approved + m.rejected ? fmt.pct(m.approved / (m.approved + m.rejected)) : "—" }),
      h("td", { class: "num", text: m.eval_runs ? fmt.num1(m.eval_score_avg) : "—" }),
      h("td", { class: "num", text: fmt.usd(m.avg_cost_usd) }),
      h("td", { class: "num", text: fmt.duration(m.avg_seconds * 1000) }));
    return h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, ["Versión", "Trabajos", "Éxito", "👍/👎", "Revisiones OK", "Nota eval.", "Coste medio", "Duración"].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", null, ms.map(row))));
  }

  O.route("/agentes/:id", {
    title: "Agente",
    mount(root, params) {
      const isNew = params.id === "nuevo";
      if (isNew) {
        O.put(root,
          ui.pageHead({ back: { href: "#/agentes", label: "Agentes" }, title: "Nuevo agente", icon: "✨", subtitle: "Dale una personalidad, un motor y un modelo. Tendrá su propio escritorio en la oficina." }),
          h("div", { class: "card" }, editorForm(null, (r) => O.go(r && r.id ? "#/agentes/" + O.enc(r.id) : "#/agentes"))));
        return {};
      }
      const id = params.id;
      const agent0 = O.sel.agent(id);
      if (!agent0) {
        O.put(root, ui.empty({ icon: "🔎", title: "Este agente no existe", actions: ui.btn("Ver agentes", { href: "#/agentes", kind: "primary" }) }));
        return {};
      }
      let dirty = false;
      let shownVersion = agent0.version;
      const markDirty = (form) => {
        form.addEventListener("input", () => { dirty = true; });
        form.addEventListener("change", () => { dirty = true; });
        form.addEventListener("click", (e) => { if (e.target.closest(".seg-btn, .chip-btn, .swatch")) dirty = true; });
      };
      const head = h("div");
      const stale = h("div");
      const formBox = h("div", { class: "card" });
      const side = h("div", { class: "side-col" });
      const histBox = h("div");
      const metricsBox = h("div");
      O.put(root, head, stale, h("div", { class: "two-col" },
        h("div", { class: "main-col" }, formBox,
          ui.section("Historial de versiones", { icon: "🕘" }, histBox),
          ui.section("Métricas por versión", { icon: "📊" }, metricsBox)),
        side));

      const drawForm = () => {
        const a = O.sel.agent(id);
        dirty = false;
        shownVersion = a.version;
        stale.replaceChildren();
        O.put(formBox, editorForm(a, () => { dirty = false; }));
        markDirty(formBox.firstChild);
      };
      const render = () => {
        const a = O.sel.agent(id);
        if (!a) {
          O.put(root, ui.empty({ icon: "👋", title: "El agente ya no existe", actions: ui.btn("Ver agentes", { href: "#/agentes", kind: "primary" }) }));
          return;
        }
        ui.memo(head, JSON.stringify([a.name, a.role, a.emoji, a.color, a.version, a.enabled]), () => ui.pageHead({
          back: { href: "#/agentes", label: "Agentes" },
          title: h("span", { class: "job-title" }, ui.avatar(a, "md"), h("span", { text: a.name })),
          subtitle: a.role + " · v" + a.version + (a.enabled ? "" : " · desactivado"),
        }));
        O.router.setTitle(a.name);
        if (a.version !== shownVersion || JSON.stringify(editable(a)) !== formBox._base) {
          if (!dirty) {
            drawForm();
            formBox._base = JSON.stringify(editable(a));
          } else {
            O.put(stale, h("div", { class: "notice notice-warn" },
              "Este agente ha cambiado en el servidor (ahora v" + a.version + "). ",
              h("button", { type: "button", class: "link-btn", on: { click: () => { drawForm(); formBox._base = JSON.stringify(editable(O.sel.agent(id))); } } }, "Descartar mis cambios y recargar")));
          }
        }
        ui.memo(histBox, JSON.stringify([a.version, a.history.length, metricsOf(id)]), () => historyPanel(a));
        ui.memo(metricsBox, JSON.stringify(metricsOf(id)), () => metricsTable(a));
        const st = O.sel.agentState(id);
        const props = S.snap.proposals.filter((p) => p.type === "prompt" && p.target_id === id && p.status === "pendiente");
        const coachJob = O.sel.jobs().find((j) => j.kind === "retro" && j.source && j.source.type === "coach" && (j.source.ref === id || (j.title || "").includes(a.name)) && !O.isFinished(j.status));
        ui.memo(side, JSON.stringify([a.enabled, st.state, props.map((p) => p.id), coachJob && coachJob.id]), () => [
          h("div", { class: "card" },
            h("h3", { class: "card-title", text: "Acciones" }),
            h("div", { class: "stack" },
              ui.btn("Encargar trabajo", { href: "#/nuevo?agente=" + O.enc(id), kind: "primary", icon: "plus" }),
              ui.btn("Ver sus trabajos", { href: "#/tablero?agente=" + O.enc(id), icon: "board" }),
              ui.btn("Verlo en la oficina", { onClick: () => { O.go("#/"); setTimeout(() => O.openAgentDrawer(id), 60); }, icon: "office" }),
              ui.btn(a.enabled ? "Desactivar" : "Activar", {
                icon: a.enabled ? "pause" : "play",
                onClick: async (e) => {
                  await ui.busy(e.currentTarget, async () => {
                    await saveAgent(a, Object.assign(editable(a), { enabled: !a.enabled }));
                    O.toast(a.enabled ? a.name + " desactivado." : a.name + " activado.", { kind: "ok" });
                  }, "No se pudo cambiar");
                },
              }),
              ui.btn("Borrar agente", {
                icon: "trash", kind: "danger-ghost",
                onClick: async (e) => {
                  const btn = e.currentTarget;
                  const ok = await O.confirm({ title: "¿Borrar a " + a.name + "?", text: "Sus trabajos pasados se conservan con la foto del agente que los hizo. Si solo quieres que descanse, desactívalo.", confirmLabel: "Borrar", danger: true, typeToConfirm: a.id });
                  if (!ok) return;
                  await ui.busy(btn, async () => {
                    await O.api.post("/api/agents/" + O.enc(id) + "/delete", {});
                    O.toast(a.name + " ya no forma parte del equipo.", { kind: "ok" });
                    O.store.sync();
                    O.go("#/agentes");
                  }, "No se pudo borrar");
                },
              }))),
          h("div", { class: "card coach-card" },
            h("h3", { class: "card-title", text: "🧭 Coach" }),
            h("p", { class: "muted small", text: "El Coach estudia los últimos trabajos de " + a.name + " (resultados, valoraciones, revisiones de sus cambios y notas) y propone un system prompt mejor. Tú decides si se aprueba: si sí, nace una versión nueva." }),
            props.length ? h("p", null, h("a", { href: "#/bandeja", text: "Hay " + props.length + " " + u.plural(props.length, "propuesta pendiente", "propuestas pendientes") + " →" })) : null,
            coachJob ? h("p", null, h("a", { href: "#/trabajo/" + O.enc(coachJob.id), text: "El Coach está en ello →" })) : null,
            ui.btn("Pedir mejora al Coach", {
              icon: "sparkle", kind: "primary",
              onClick: async (e) => {
                await ui.busy(e.currentTarget, async () => {
                  const r = await O.api.post("/api/agents/" + O.enc(id) + "/coach", {});
                  const j = r && r.id ? r : r && r.job;
                  if (j && j.id) O.store.upsertJob(j);
                  O.toast("El Coach analizará a " + a.name + ". Su propuesta llegará a la Bandeja.", {
                    kind: "ok", action: j && j.id ? { label: "Ver", fn: () => O.go("#/trabajo/" + O.enc(j.id)) } : null,
                  });
                }, "No se pudo avisar al Coach");
              },
            })),
        ]);
      };
      formBox._base = JSON.stringify(editable(agent0));
      O.put(formBox, editorForm(agent0, () => { dirty = false; }));
      markDirty(formBox.firstChild);
      render();
      return { update: render };
    },
  });
})();
