/*
 * Oficina de agentes · AX (#/ax, #/ax/tarea/<name>): the raw view of the AX
 * lab (tasks with suspend / resume / delete, workspaces, gateways). Data is
 * read on demand; deleting a task requires typing its exact name.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, ui } = O;
  const S = O.state;

  function axIntro() {
    const ax = S.snap.ax || {};
    return h("div", { class: "notice notice-info" },
      h("strong", { text: "AX " }), "es el laboratorio de sandboxes (gVisor) donde corren los agentes: un único worker, un trabajo cada vez. ",
      "Aquí ves sus objetos en bruto. Estado: ",
      h("strong", { text: ax.ready ? "listo" : "no listo" }),
      ax.blocking ? " · ocupado por la tarea del host " + ax.blocking : "",
      ax.cleanup_failed ? " · limpieza pendiente de " + ax.cleanup_failed : "",
      ax.reap_note ? " · " + ax.reap_note : "");
  }

  function table(headers, rows) {
    return h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, headers.map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", null, c)))))));
  }

  function tasksPanel() {
    const box = h("div");
    let offset = 0;
    const load = async () => {
      O.put(box, ui.loading("Leyendo las tareas de AX…"));
      try {
        const d = await O.api.get("/api/tasks?offset=" + offset);
        const tasks = Array.isArray(d.tasks) ? d.tasks : [];
        O.put(box,
          h("div", { class: "section-actions-row" }, ui.btn("Actualizar", { size: "sm", kind: "ghost", icon: "refresh", onClick: load })),
          tasks.length ? table(["Nombre", "Fase", "Creada", ""], tasks.map((t) => [
            h("a", { href: "#/ax/tarea/" + O.enc(t.name), class: "mono", text: t.name }),
            t.phase || "—",
            ui.time(t.created),
            t.panel_run ? h("span", { class: "badge badge-live", text: "ejecución de la Oficina" }) : "",
          ])) : ui.empty({ cls: "empty-sm", icon: "🫙", text: "No hay tareas en AX. Las de la Oficina se llaman web-…; las del host, tarea-…" }),
          h("div", { class: "row-actions" },
            offset > 0 ? ui.btn("Anteriores", { size: "sm", onClick: () => { offset = Math.max(0, offset - 50); load(); } }) : null,
            d.next !== undefined ? ui.btn("Siguientes", { size: "sm", onClick: () => { offset = d.next; load(); } }) : null));
      } catch (e) {
        O.put(box, h("p", { class: "field-error", text: e.message }), ui.btn("Reintentar", { size: "sm", onClick: load }));
      }
    };
    load();
    return box;
  }

  function listPanel(kind) {
    const box = h("div");
    const load = async () => {
      O.put(box, ui.loading());
      try {
        const d = await O.api.get("/api/" + kind);
        const head = h("div", { class: "section-actions-row" }, ui.btn("Actualizar", { size: "sm", kind: "ghost", icon: "refresh", onClick: load }));
        if (kind === "gateways") {
          const list = Array.isArray(d.gateways) ? d.gateways : [];
          O.put(box, head, list.length ? table(["Nombre", "Listeners", "Salida permitida", "Creado"], list.map((g) => [
            h("span", { class: "mono", text: g.name }),
            (g.listeners || []).map((l) => l.name + ":" + l.port + "/" + l.protocol).join(", ") || "—",
            (g.egress || []).map((x) => x.host + ":" + x.port).join(", ") || "—",
            ui.time(g.created),
          ])) : ui.empty({ cls: "empty-sm", icon: "🚪", text: "No hay gateways." }));
        } else {
          const list = Array.isArray(d.workspaces) ? d.workspaces : [];
          O.put(box, head, list.length ? table(["Nombre", "Repositorios", "MCP", "Creado"], list.map((w) => [
            h("span", { class: "mono", text: w.name }),
            (w.git || []).map((g) => g.repo + (g.branch ? " (" + g.branch + ")" : "")).join(", ") || "—",
            String(w.mcp_servers || 0),
            ui.time(w.created),
          ])) : ui.empty({ cls: "empty-sm", icon: "🗂️", text: "No hay workspaces." }));
        }
      } catch (e) {
        O.put(box, h("p", { class: "field-error", text: e.message }), ui.btn("Reintentar", { size: "sm", onClick: load }));
      }
    };
    load();
    return box;
  }

  O.route("/ax", {
    title: "AX",
    mount(root) {
      const tabs = ui.tabs({
        tabs: [
          { id: "tareas", label: "Tareas", render: tasksPanel },
          { id: "workspaces", label: "Workspaces", render: () => listPanel("workspaces") },
          { id: "gateways", label: "Gateways", render: () => listPanel("gateways") },
        ],
      });
      O.put(root, ui.pageHead({ title: "AX", icon: "🖥️", subtitle: "La vista técnica del laboratorio de sandboxes." }), axIntro(), tabs.el);
      return {};
    },
  });

  O.route("/ax/tarea/:name", {
    title: "Tarea de AX",
    mount(root, params) {
      const name = params.name;
      let poll = null;
      const body = h("div");
      O.put(root, ui.pageHead({ back: { href: "#/ax", label: "AX" }, title: h("span", { class: "mono", text: name }), icon: "📦" }), body);
      const load = async () => {
        O.put(body, ui.loading());
        let t;
        try {
          t = await O.api.get("/api/tasks/" + O.enc(name));
        } catch (e) {
          O.put(body, ui.empty({ icon: "🔎", title: e.status === 404 ? "La tarea no existe" : "No se pudo leer la tarea", text: e.message, actions: ui.btn("Volver a AX", { href: "#/ax", kind: "primary" }) }));
          return;
        }
        const res = t.resources || {};
        const msg = h("p", { class: "muted", role: "status" });
        const act = async (action, btn, payload) => {
          await ui.busy(btn, async () => {
            const r = await O.api.post("/api/tasks/" + O.enc(name) + "/" + action, payload || {});
            if (action === "delete") {
              if (r.deleted) {
                O.toast("Tarea " + name + " borrada.", { kind: "ok" });
                O.go("#/ax");
                return;
              }
              msg.textContent = r.message || "AX aún está borrándola.";
              waitGone(18);
              return;
            }
            O.toast(action === "suspend" ? "Tarea suspendida." : "Tarea reanudada.", { kind: "ok" });
            load();
          }, "AX no aceptó la operación");
        };
        // After a 202 the task is checked every 5 s until it is gone.
        const waitGone = (tries) => {
          poll = setTimeout(async () => {
            try {
              await O.api.get("/api/tasks/" + O.enc(name));
              if (tries > 1) waitGone(tries - 1);
              else msg.textContent = "La tarea sigue existiendo; vuelve a intentarlo.";
            } catch (e) {
              if (e.status === 404) {
                O.toast("Tarea " + name + " borrada.", { kind: "ok" });
                O.go("#/ax");
              } else if (tries > 1) waitGone(tries - 1);
            }
          }, 5000);
        };
        const confirmInput = ui.input({ class: "input mono", placeholder: name, autocomplete: "off", spellcheck: "false", "aria-label": "Nombre de la tarea para confirmar" });
        const delBtn = ui.btn("Borrar", {
          kind: "danger", icon: "trash", disabled: t.panel_run || null,
          onClick: (e) => {
            if (confirmInput.value !== name) {
              msg.textContent = "Escribe el nombre exacto de la tarea para confirmar.";
              confirmInput.focus();
              return;
            }
            act("delete", e.currentTarget, { confirm: confirmInput.value });
          },
        });
        O.put(body,
          h("div", { class: "card" }, table(["Campo", "Valor"], [
            ["Fase", t.phase || "—"],
            ["Creada", ui.time(t.created, { absolute: true })],
            ["Imagen", h("span", { class: "mono", text: t.image || "—" })],
            ["Suspendida", t.suspend ? "sí" : "no"],
            ["Actor", t.actor || "—"],
            ["Recursos (AX no los aplica)", (res.limits_cpu || "—") + " CPU · " + (res.limits_memory || "—")],
            ["Workspaces", (t.workspaces || []).map((w) => w.name + (w.path ? " → " + w.path : "")).join(", ") || "—"],
            ["Variables", (t.env || []).map((e) => e.name + "=" + e.value).join(", ") || "—"],
            ["Gateway", t.gateway || "—"],
          ])),
          ui.section("Condiciones", null, (t.conditions || []).length
            ? table(["Tipo", "Estado", "Motivo", "Mensaje", "Cambio"], t.conditions.map((c) => [c.type, c.status, c.reason || "", c.message || "", ui.time(c.changed)]))
            : h("p", { class: "muted", text: "Sin condiciones." })),
          ui.section("Acciones", null,
            h("div", { class: "row-actions" },
              ui.btn("Suspender", { icon: "pause", disabled: !t.can_suspend || null, onClick: (e) => act("suspend", e.currentTarget) }),
              ui.btn("Reanudar", { icon: "play", disabled: !t.can_resume || null, onClick: (e) => act("resume", e.currentTarget) })),
            t.panel_run ? h("p", { class: "muted", text: "Es la ejecución en curso de la Oficina: Suspender y Borrar están desactivados; cancélala desde su trabajo." }) : null,
            h("div", { class: "card danger-zone" },
              h("h3", { class: "card-title", text: "Borrar la tarea" }),
              h("p", { text: "Escribe el nombre exacto de la tarea para confirmar. AX la borra (y su workspace web-/tarea-) y se comprueba que de verdad desaparece." }),
              h("div", { class: "inline-row" }, confirmInput, delBtn),
              msg)));
      };
      load();
      return {
        unmount() {
          clearTimeout(poll);
        },
      };
    },
  });
})();
