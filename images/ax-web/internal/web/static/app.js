"use strict";
// AX panel. Everything is rendered with textContent: agent output and AX
// data are never interpreted as HTML. Traefik allows 30 requests a minute
// per address, so the page never polls fast: run state arrives over SSE.

const main = document.getElementById("main");
let source = null;
let poller = null;

const STATES = {
  preparing: "preparando", waiting: "esperando al sandbox", running: "en marcha",
  cancelling: "cancelando", cleaning: "borrando la tarea",
  cleanup_failed: "NO SE PUDO BORRAR (se reintenta)", finished: "terminada",
};

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v !== undefined && v !== null) node.setAttribute(k, String(v));
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-AX-Web"] = "1";
    opts.body = JSON.stringify(body || {});
  }
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (e) { data = {}; }
  if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
  return data;
}

function table(headers, rows) {
  const t = el("table", {}, el("tr", {}, ...headers.map((h) => el("th", { text: h }))));
  for (const r of rows) t.append(el("tr", {}, ...r.map((c) => el("td", {}, c))));
  return t;
}

function fail(err) {
  main.append(el("p", { class: "error", text: err.message }));
}

function stopLive() {
  if (source) { source.close(); source = null; }
  if (poller) { clearTimeout(poller); poller = null; }
}

async function banners() {
  const box = document.getElementById("banners");
  try {
    const s = await api("GET", "/api/status");
    box.replaceChildren();
    const add = (text, red) => box.append(el("div", { class: red ? "banner red" : "banner", text }));
    if (s.in_blackout) add("Ventana del Observatorio (" + s.blackout + " UTC): no se lanzan ejecuciones.");
    if (s.manager.reap_note) add(s.manager.reap_note, true);
    if (s.manager.cleanup_failed) add("La tarea " + s.manager.cleanup_failed + " no se pudo borrar; se reintenta cada 30 s.", true);
    for (const w of s.warnings || []) add(w, true);
    return s;
  } catch (e) {
    box.replaceChildren(el("div", { class: "banner red", text: "No se pudo leer el estado: " + e.message }));
    return null;
  }
}

async function viewTasks(offset) {
  main.replaceChildren(el("h1", { text: "Tareas" }));
  try {
    const d = await api("GET", "/api/tasks?offset=" + (offset || 0));
    main.append(table(["Nombre", "Fase", "Creada", "Panel"], d.tasks.map((t) => [
      el("a", { href: "#/tarea/" + encodeURIComponent(t.name), text: t.name }),
      t.phase || "-", t.created || "-", t.panel_run ? "ejecución en curso" : "",
    ])));
    if (d.next !== undefined) main.append(el("button", { text: "Siguientes", on: { click: () => viewTasks(d.next) } }));
  } catch (e) { fail(e); }
}

async function viewTask(name) {
  main.replaceChildren(el("h1", { text: "Tarea " + name }));
  let t;
  try { t = await api("GET", "/api/tasks/" + encodeURIComponent(name)); } catch (e) { fail(e); return; }
  const msg = el("p");
  const act = async (action, body) => {
    try {
      const r = await api("POST", "/api/tasks/" + encodeURIComponent(name) + "/" + action, body);
      if (action === "delete") {
        if (r.deleted) { location.hash = "#/tareas"; return; }
        msg.textContent = r.message;
        waitGone(name, msg, 18);
        return;
      }
      viewTask(name);
    } catch (e) { msg.className = "error"; msg.textContent = e.message; }
  };
  const res = t.resources || {};
  main.append(table(["Campo", "Valor"], [
    ["Fase", t.phase || "-"], ["Creada", t.created || "-"], ["Imagen", t.image || "-"],
    ["Suspendida", t.suspend ? "sí" : "no"], ["Actor", t.actor || "-"],
    ["Recursos (no aplicados por AX)", (res.limits_cpu || "-") + " CPU, " + (res.limits_memory || "-")],
    ["Workspaces", t.workspaces.map((w) => w.name + " → " + (w.path || "")).join(", ") || "-"],
    ["Variables", t.env.map((e) => e.name + "=" + e.value).join(", ") || "-"],
  ]));
  main.append(el("h2", { text: "Condiciones" }), table(["Tipo", "Estado", "Motivo", "Mensaje"],
    t.conditions.map((c) => [c.type, c.status, c.reason || "", c.message || ""])));
  main.append(
    el("button", { text: "Suspender", disabled: !t.can_suspend, on: { click: () => act("suspend") } }),
    el("button", { text: "Reanudar", disabled: !t.can_resume, on: { click: () => act("resume") } }));
  if (t.panel_run) main.append(el("p", { class: "muted", text: "Ejecución del panel en curso: Suspender está desactivado; usa Cancelar en la salida en directo." }));
  const confirm = el("input", { placeholder: name, autocomplete: "off" });
  main.append(el("h2", { text: "Borrar" }),
    el("p", { text: "Escribe el nombre de la tarea para confirmar. Se espera a que AX la borre de verdad." }),
    confirm, el("br"),
    el("button", { class: "danger", text: "Borrar", disabled: t.panel_run, on: { click: () => act("delete", { confirm: confirm.value }) } }),
    msg);
}

// Checks every 5 s, up to tries times, until the task no longer exists.
function waitGone(name, msg, tries) {
  poller = setTimeout(async () => {
    try {
      await api("GET", "/api/tasks/" + encodeURIComponent(name));
      if (tries > 1) waitGone(name, msg, tries - 1);
      else { msg.className = "error"; msg.textContent = "La tarea sigue existiendo; vuelve a intentarlo."; }
    } catch (e) {
      if (e.message === "no existe") location.hash = "#/tareas";
      else if (tries > 1) waitGone(name, msg, tries - 1);
    }
  }, 5000);
}

async function viewNew() {
  main.replaceChildren(el("h1", { text: "Nueva ejecución" }));
  const s = await banners();
  const lim = (s && s.limits) || { max_turns: 50, max_timeout_minutes: 45, repo_hosts: ["github.com"], cpu: ["1"], memory: ["1Gi"] };
  const f = {
    repo: el("input", { placeholder: "https://" + lim.repo_hosts[0] + "/propietario/repositorio", required: true, maxlength: 300 }),
    branch: el("input", { value: "main", maxlength: 100 }),
    prompt: el("textarea", { required: true }),
    turns: el("input", { type: "number", min: 1, max: lim.max_turns, value: 20 }),
    timeout: el("input", { type: "number", min: 5, max: lim.max_timeout_minutes, value: Math.min(30, lim.max_timeout_minutes) }),
    cpu: el("select", {}, ...lim.cpu.map((c) => el("option", { value: c, selected: c === "1", text: c }))),
    memory: el("select", {}, ...lim.memory.map((m) => el("option", { value: m, selected: m === "1Gi", text: m }))),
  };
  const msg = el("p", { class: "error" });
  const form = el("form", {},
    el("label", { text: "Repositorio público (https)" }), f.repo,
    el("label", { text: "Rama" }), f.branch,
    el("label", { text: "Instrucción" }), f.prompt,
    el("label", { text: "Agente" }), el("select", { disabled: true }, el("option", { text: "Claude" })),
    el("label", { text: "Turnos máximos (1-" + lim.max_turns + ")" }), f.turns,
    el("label", { text: "Tiempo máximo en minutos (5-" + lim.max_timeout_minutes + ")" }), f.timeout,
    el("label", { text: "CPU (AX no la aplica: el límite real es el del worker)" }), f.cpu,
    el("label", { text: "Memoria (AX no la aplica: el límite real es el del worker)" }), f.memory,
    el("br"), el("button", { type: "submit", text: "Lanzar" }), msg);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    msg.textContent = "";
    try {
      await api("POST", "/api/runs", {
        repo: f.repo.value.trim(), branch: f.branch.value.trim(), prompt: f.prompt.value, agent: "claude",
        turns: Number(f.turns.value), timeout_minutes: Number(f.timeout.value),
        cpu: f.cpu.value, memory: f.memory.value,
      });
      location.hash = "#/ejecucion";
    } catch (e) { msg.textContent = e.message; }
  });
  main.append(form);
}

function describe(run) {
  let text = run.id + " — " + (STATES[run.state] || run.state) + " — " + run.repo + " (" + run.branch + ")";
  if (run.exit_code !== undefined && run.exit_code !== null) text += " — código " + run.exit_code;
  return text;
}

async function viewRun() {
  main.replaceChildren(el("h1", { text: "Salida en directo" }));
  let d;
  try { d = await api("GET", "/api/runs/current"); } catch (e) { fail(e); return; }
  if (!d.run) { main.append(el("p", { text: "No hay ninguna ejecución." })); return; }
  const run = d.run;
  const line = el("p", { text: describe(run) });
  const msg = el("p");
  const out = el("pre", { id: "out" });
  const cancel = el("button", { class: "danger", text: "Cancelar", disabled: run.state === "finished", on: { click: async () => {
    try { await api("POST", "/api/runs/" + encodeURIComponent(run.id) + "/cancel"); } catch (e) { msg.className = "error"; msg.textContent = e.message; }
  } } });
  main.append(line, cancel, msg, out);
  source = new EventSource("/api/runs/" + encodeURIComponent(run.id) + "/events");
  source.addEventListener("out", (ev) => {
    const c = JSON.parse(ev.data);
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 4;
    out.append(el("span", { class: c.s, text: c.d }));
    if (stick) out.scrollTop = out.scrollHeight;
  });
  source.addEventListener("truncated", () => out.append(el("span", { class: "sys", text: "[se perdió parte de la salida más antigua]\n" })));
  source.addEventListener("state", (ev) => {
    const v = JSON.parse(ev.data);
    line.textContent = describe(v);
    cancel.disabled = ["cleaning", "cleanup_failed", "finished"].includes(v.state);
  });
  source.addEventListener("end", (ev) => {
    const v = JSON.parse(ev.data);
    line.textContent = describe(v);
    cancel.disabled = true;
    stopLive();
  });
}

async function viewList(kind) {
  main.replaceChildren(el("h1", { text: kind === "gateways" ? "Gateways" : "Workspaces" }));
  try {
    const d = await api("GET", "/api/" + kind);
    if (kind === "gateways") {
      main.append(table(["Nombre", "Listeners", "Salida permitida"], d.gateways.map((g) => [g.name,
        g.listeners.map((l) => l.name + ":" + l.port + "/" + l.protocol).join(", "),
        g.egress.map((h) => h.host + ":" + h.port).join(", ")])));
    } else {
      main.append(table(["Nombre", "Repositorios", "Creado"], d.workspaces.map((w) => [w.name,
        w.git.map((g) => g.repo + (g.branch ? " (" + g.branch + ")" : "")).join(", "), w.created || "-"])));
    }
  } catch (e) { fail(e); }
}

function route() {
  stopLive();
  const h = location.hash || "#/tareas";
  if (h.startsWith("#/tarea/")) viewTask(decodeURIComponent(h.slice(8)));
  else if (h === "#/nueva") viewNew();
  else if (h === "#/ejecucion") viewRun();
  else if (h === "#/gateways") viewList("gateways");
  else if (h === "#/workspaces") viewList("workspaces");
  else viewTasks(0);
}

window.addEventListener("hashchange", route);
banners();
setInterval(banners, 60000);
route();
