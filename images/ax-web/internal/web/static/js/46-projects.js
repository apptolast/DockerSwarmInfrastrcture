/*
 * Oficina de agentes · Proyectos (#/proyectos, #/proyectos/<id>,
 * #/proyectos/nuevo): repositories, notes, the memory ledger and GitHub.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, u, fmt, ui } = O;
  const S = O.state;
  const P = (id, op) => "/api/projects/" + O.enc(id) + (op ? "/" + op : "");

  function projectBody(p, over) {
    return Object.assign({
      name: p.name, repo: p.repo, branch: p.branch, description: p.description || "",
      service: p.service || "", url: p.url || "", notes: p.notes || "", archived: !!p.archived,
    }, over || {});
  }

  async function updateProject(p, over) {
    const r = await O.api.post(P(p.id), projectBody(p, over));
    O.store.sync();
    return r;
  }

  function repoLink(p) {
    return h("a", { href: p.repo, target: "_blank", rel: "noopener noreferrer", class: "mono repo-link" }, p.repo.replace(/^https:\/\/(www\.)?github\.com\//, ""), " ", O.icon("external", { size: 13 }));
  }

  function badges(p) {
    return h("span", { class: "chips" },
      p.seeded ? h("span", { class: "badge badge-soft", title: "Viene de la configuración revisada del servidor: solo se puede archivar", text: "⚙️ configuración" }) : null,
      p.archived ? h("span", { class: "badge badge-warn", text: "archivado" }) : null,
      p.service ? h("span", { class: "badge badge-soft mono", title: "Servicio de Swarm", text: "🐳 " + p.service }) : null);
  }

  // ------------------------------------------------- import from GitHub --

  const DEFAULT_OWNER = "apptolast";
  const OWNER_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/;
  // Same repository for GitHub: case-insensitive, without ".git" or "/".
  const repoKey = (url) => String(url || "").trim().toLowerCase().replace(/\/+$/, "").replace(/\.git$/, "");
  // One line without control characters, at most n runes (server rules).
  const oneLineCut = (s, n) => Array.from(String(s || "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").replace(/\s+/g, " ").trim())
    .slice(0, n).join("").trim();

  function projectFromRepo(r) {
    return {
      name: oneLineCut(r.name, 60),
      repo: r.html_url,
      branch: r.default_branch || "",
      description: oneLineCut(r.description, 300),
      service: "", url: "", notes: "",
    };
  }

  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

  /** POST /api/projects, waiting and retrying when the edge says 429. */
  async function createProject(body) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await O.api.post("/api/projects", body);
      } catch (e) {
        if (e && e.status === 429 && attempt < 2) {
          await sleep(2500 * (attempt + 1));
          continue;
        }
        throw e;
      }
    }
  }

  /**
   * Dialog: list an owner's GitHub repositories and add the chosen ones as
   * projects, one request at a time, then reload the snapshot once.
   */
  function importDialog() {
    const st = { repos: [], rows: [], running: false, loading: false, owner: "" };
    const owner = ui.input({ value: DEFAULT_OWNER, maxlength: 39, class: "input mono", autocomplete: "off", spellcheck: "false", autofocus: true });
    const loadBtn = ui.btn("Cargar repos", { type: "submit", icon: "refresh" });
    const ownerForm = h("form", { class: "import-owner", novalidate: true },
      ui.field("Propietario en GitHub (usuario u organización)", owner, { field: "owner", cls: "grow" }), loadBtn);
    const search = ui.input({ type: "search", placeholder: "Buscar por nombre, descripción o lenguaje", "aria-label": "Buscar repositorios", autocomplete: "off" });
    const info = h("div", { class: "import-info" });
    const list = h("div", { class: "checks import-list", role: "group", "aria-label": "Repositorios" });
    const count = h("span", { class: "muted small", "aria-live": "polite" });
    const markAll = h("button", { type: "button", class: "link-btn", on: { click: () => setAll(true) } }, "Marcar los visibles");
    const markNone = h("button", { type: "button", class: "link-btn", on: { click: () => setAll(false) } }, "Desmarcar todos");
    const tools = h("div", { class: "import-tools", hidden: true }, search, h("div", { class: "import-tools-row" }, count, h("span", { class: "spacer" }), markAll, markNone));

    function existing() {
      const m = new Map();
      for (const p of S.snap.projects || []) m.set(repoKey(p.repo), p);
      return m;
    }

    const selectable = (r) => !r.why && !r.done;
    function updateCount() {
      const sel = st.rows.filter((r) => r.cb.checked && selectable(r)).length;
      const vis = st.rows.filter((r) => !r.el.hidden).length;
      count.textContent = sel + " " + u.plural(sel, "seleccionado", "seleccionados") + " · " + vis + " de " + st.rows.length + " " + u.plural(st.rows.length, "repo", "repos");
    }
    function setAll(on) {
      if (st.running) return;
      for (const r of st.rows) if (selectable(r) && (!on || !r.el.hidden)) r.cb.checked = on;
      updateCount();
    }
    function filter() {
      const q = search.value.trim().toLowerCase();
      for (const r of st.rows) {
        const x = r.repo;
        r.el.hidden = !!q && ![x.name, x.full_name, x.description, x.language].some((v) => String(v || "").toLowerCase().includes(q));
      }
      updateCount();
    }
    search.addEventListener("input", filter);

    function setStatus(r, kind, text) {
      r.status.className = "import-status small is-" + kind;
      r.status.textContent = text;
      r.status.hidden = !text;
    }

    function repoRow(x, have) {
      const key = repoKey(x.html_url);
      const prev = have.get(key);
      const urlOK = ui.valid.repo(String(x.html_url || ""));
      const why = x.private ? "AX clona sin credenciales: solo repos públicos."
        : prev ? "Ya está en la Oficina como «" + prev.name + "»" + (prev.archived ? " (archivado)." : ".")
          : !urlOK ? "Su URL no es un repositorio admitido (" + ((S.snap.limits && S.snap.limits.repo_hosts) || []).join(", ") + ")." : "";
      const cb = h("input", { type: "checkbox", disabled: why ? true : null });
      cb.addEventListener("change", updateCount);
      const status = h("span", { class: "import-status small", hidden: true, role: "status" });
      const el = h("label", { class: ["check-row", "import-row", why && "is-off"] }, cb,
        h("span", { class: "import-main" },
          h("span", { class: "import-name" },
            h("strong", { class: "mono", text: x.name || x.full_name || "?" }),
            x.private ? h("span", { class: "badge badge-warn", text: "privado" }) : null,
            prev ? h("span", { class: "badge badge-ok", text: "ya añadido" }) : null,
            x.archived ? h("span", { class: "badge badge-warn", title: "Archivado en GitHub: solo lectura", text: "archivado" }) : null,
            x.fork ? h("span", { class: "badge badge-soft", title: "Es un fork de otro repositorio", text: "fork" }) : null,
            x.language ? h("span", { class: "badge badge-soft", text: x.language }) : null),
          x.description ? h("span", { class: "import-desc", text: u.trunc(u.oneLine(x.description), 200) }) : null,
          h("span", { class: "muted small" },
            h("span", { class: "mono", text: x.default_branch || "main" }),
            x.pushed_at ? [" · último push ", ui.time(x.pushed_at)] : null),
          why ? h("span", { class: "import-why small", text: why }) : null,
          status));
      return { repo: x, cb, el, status, why, done: false };
    }

    async function load() {
      const o = owner.value.trim();
      ui.fieldError(ownerForm, null);
      if (!OWNER_RE.test(o)) {
        ui.fieldError(ownerForm, { field: "owner", message: "Un usuario u organización de GitHub: letras, números y guiones." });
        return;
      }
      st.owner = o;
      st.loading = true;
      st.rows = [];
      tools.hidden = true;
      O.put(info, ui.loading("Leyendo los repos de " + o + " en GitHub…"));
      list.replaceChildren();
      await ui.busy(loadBtn, async () => {
        try {
          const d = await O.api.get("/api/github/repos?owner=" + O.enc(o));
          if (st.owner !== o) return;
          const repos = (Array.isArray(d.repos) ? d.repos : []).filter((x) => x && typeof x === "object");
          st.repos = u.sortBy(repos, (x) => u.ms(x.pushed_at) || 0, true);
          const have = existing();
          st.rows = st.repos.map((x) => repoRow(x, have));
          const pub = st.repos.filter((x) => !x.private).length;
          O.put(info, st.repos.length
            ? h("p", { class: "muted small" },
              fmt.num(st.repos.length) + " " + u.plural(st.repos.length, "repositorio", "repositorios") + " de ", h("strong", { text: d.owner || o }),
              " (" + fmt.num(pub) + " " + u.plural(pub, "público", "públicos") + "). ",
              d.authenticated ? "Leídos con el token de GitHub de la Oficina." : "Leídos sin token: GitHub solo muestra los públicos y limita las consultas.")
            : ui.empty({ cls: "empty-sm", icon: "🐙", text: (d.owner || o) + " no tiene repositorios visibles." }));
          O.put(list, st.rows.map((r) => r.el));
          tools.hidden = !st.rows.length;
          search.value = "";
          filter();
        } catch (e) {
          O.put(info, h("p", { class: "field-error", role: "alert", text: "No se pudo leer GitHub: " + e.message }));
        } finally {
          st.loading = false;
        }
      });
    }
    ownerForm.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!st.running && !st.loading) load();
    });

    function lock(on) {
      st.running = on;
      loadBtn.disabled = on;
      owner.disabled = on;
      for (const r of st.rows) if (selectable(r)) r.cb.disabled = on;
    }

    async function run() {
      if (st.running || st.loading) return false;
      const todo = st.rows.filter((r) => r.cb.checked && selectable(r));
      if (!todo.length) {
        O.toast("Marca al menos un repositorio público.", { kind: "warn" });
        return false;
      }
      lock(true);
      const release = O.store.hold();
      let ok = 0, bad = 0, last = 0;
      try {
        for (const r of todo) setStatus(r, "wait", "En espera…");
        for (const r of todo) {
          // Sequential, a few per second at most: the edge limits the host.
          const wait = last + 350 - Date.now();
          if (wait > 0) await sleep(wait);
          last = Date.now();
          setStatus(r, "busy", "Añadiendo…");
          try {
            const p = await createProject(projectFromRepo(r.repo));
            ok++;
            r.done = true;
            r.cb.checked = false;
            r.cb.disabled = true;
            r.el.classList.add("is-off");
            setStatus(r, "ok", "✅ Añadido" + (p && p.id ? " como «" + p.id + "»" : "") + ".");
          } catch (e) {
            bad++;
            setStatus(r, "bad", "⛔ " + (e && e.message ? e.message : String(e)) + (e && e.field ? " (" + e.field + ")" : ""));
          }
        }
      } finally {
        lock(false);
        release();
        // One snapshot reload for the whole batch.
        if (ok) O.store.refresh();
        updateCount();
      }
      if (ok) O.toast(fmt.num(ok) + " " + u.plural(ok, "proyecto añadido", "proyectos añadidos") + (bad ? "; " + bad + " " + u.plural(bad, "falló", "fallaron") + " (mira el detalle)." : "."), { kind: bad ? "warn" : "ok" });
      else if (bad) O.toast("No se pudo añadir ninguno: mira el motivo junto a cada repo.", { kind: "error" });
      return bad ? false : true;
    }

    O.dialog({
      title: "Importar proyectos desde GitHub",
      wide: true,
      body: h("div", { class: "form import-dialog" },
        h("p", { class: "muted", text: "Elige repositorios públicos de un usuario u organización: cada uno se añade como proyecto con su rama por defecto y su descripción. Los privados no se pueden añadir porque AX los clona sin credenciales." }),
        ownerForm, info, tools, list),
      actions: [
        { label: "Cerrar", kind: "ghost" },
        { label: "Añadir los marcados", kind: "primary", onClick: run },
      ],
    });
    load();
  }
  O.importProjects = importDialog;

  // ---------------------------------------------------------------- list --

  function projectCard(p) {
    const jobs = O.sel.jobs().filter((j) => j.project_id === p.id);
    const active = (p.memory || []).filter((m) => m.active).length;
    const last = jobs[0];
    return h("article", { class: ["card", "project-tile", p.archived && "is-off"] },
      h("div", { class: "card-head" },
        h("span", { class: "card-icon", "aria-hidden": "true", text: "📁" }),
        h("div", { class: "card-head-text" },
          h("h3", { class: "card-title" }, h("a", { href: "#/proyectos/" + O.enc(p.id), text: p.name })),
          h("p", { class: "card-sub" }, repoLink(p), h("span", { class: "muted mono", text: " · " + (p.branch || "main") })))),
      badges(p),
      p.description ? h("p", { class: "project-desc", text: u.trunc(p.description, 220) }) : null,
      h("p", { class: "muted small" },
        fmt.num(jobs.length) + " " + u.plural(jobs.length, "trabajo", "trabajos") + " · " + active + " " + u.plural(active, "lección", "lecciones") + " en memoria",
        last ? [" · último ", ui.time(last.created)] : null,
        p.url ? [" · ", h("a", { href: p.url, target: "_blank", rel: "noopener noreferrer", text: p.url.replace(/^https:\/\//, "") })] : null),
      h("div", { class: "row-actions" },
        p.archived ? null : ui.btn("Encargar", { href: "#/nuevo?proyecto=" + O.enc(p.id), size: "sm", icon: "plus" }),
        ui.btn("Abrir", { href: "#/proyectos/" + O.enc(p.id), size: "sm", kind: "ghost" })));
  }

  O.route("/proyectos", {
    title: "Proyectos",
    mount(root) {
      let showArchived = false;
      const body = h("div");
      const toggle = h("label", { class: "switch-row small" },
        h("input", { type: "checkbox", class: "switch", on: { change: (e) => { showArchived = e.currentTarget.checked; body._sig = null; render(); } } }),
        h("span", { text: "Mostrar archivados" }));
      O.put(root, ui.pageHead({
        title: "Proyectos", icon: "📁",
        subtitle: "Repositorios públicos de GitHub en los que trabajan los agentes. Cada encargo los clona en un sandbox efímero; nada se publica sin que tú lo pidas.",
        actions: [toggle,
          ui.btn("Importar desde GitHub", { icon: "download", onClick: () => importDialog() }),
          ui.btn("Nuevo proyecto", { href: "#/proyectos/nuevo", kind: "primary", icon: "plus" })],
      }), body);
      const render = () => {
        const list = S.snap.projects.filter((p) => showArchived || !p.archived);
        ui.memo(body, JSON.stringify([showArchived, S.snap.projects, O.state.jobsVersion]), () => list.length
          ? h("div", { class: "tile-grid" }, list.map(projectCard))
          : ui.empty({ icon: "📁", title: "No hay proyectos", text: "Añade un repositorio público de GitHub para empezar a encargar trabajo.", actions: [
            ui.btn("Importar desde GitHub", { icon: "download", onClick: () => importDialog() }),
            ui.btn("Nuevo proyecto", { href: "#/proyectos/nuevo", kind: "primary" })] }));
      };
      render();
      return { update: render };
    },
  });

  // -------------------------------------------------------------- editor --

  function projectForm(p, onSaved) {
    const form = h("form", { class: "form", novalidate: true });
    const seeded = p && p.seeded;
    const id = p ? null : ui.input({ maxlength: 32, class: "input mono", placeholder: "se genera del nombre", autocomplete: "off", spellcheck: "false" });
    const name = ui.input({ maxlength: 60, value: p ? p.name : "", disabled: seeded || null });
    const repo = ui.input({ maxlength: 300, value: p ? p.repo : "", placeholder: "https://github.com/propietario/repositorio", class: "input mono", disabled: seeded || null, autocomplete: "off", spellcheck: "false" });
    const branch = ui.input({ maxlength: 200, value: p ? p.branch : "main", class: "input mono", disabled: seeded || null, autocomplete: "off", spellcheck: "false" });
    const desc = ui.textarea({ rows: 3, maxlength: 300, disabled: seeded || null });
    desc.value = p ? p.description || "" : "";
    const service = ui.input({ maxlength: 80, value: p ? p.service || "" : "", class: "input mono", disabled: seeded || null, placeholder: "opcional" });
    const url = ui.input({ maxlength: 300, value: p ? p.url || "" : "", class: "input mono", disabled: seeded || null, placeholder: "https://… (opcional)" });
    const submit = seeded ? null : ui.btn(p ? "Guardar" : "Crear proyecto", { type: "submit", kind: "primary", icon: "check" });
    O.put(form,
      seeded ? h("div", { class: "notice notice-info", text: "Este proyecto viene de la configuración revisada del servidor (config/ax-lab.yml): sus datos se cambian ahí. Aquí puedes editar sus notas, su memoria o archivarlo." }) : null,
      h("div", { class: "grid-2" },
        ui.field("Nombre", name, { field: "name" }),
        id ? ui.field("Identificador", id, { field: "id", optional: true, hint: "Minúsculas, números y guiones." }) : null),
      ui.field("Repositorio (público, https)", repo, { field: "repo", hint: "AX lo clona sin credenciales: tiene que ser público." }),
      h("div", { class: "grid-3" },
        ui.field("Rama por defecto", branch, { field: "branch" }),
        ui.field("Servicio de Swarm", service, { field: "service", optional: true }),
        ui.field("URL pública", url, { field: "url", optional: true })),
      ui.field("Descripción", desc, { field: "description", optional: true, counter: ui.counter(desc, 300, "runes") }),
      submit ? h("div", { class: "form-foot" }, h("span"), submit) : null);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ui.fieldError(form, null);
      const err = (field, message) => ui.fieldError(form, { field, message });
      if (!name.value.trim()) return err("name", "Ponle un nombre.");
      if (id && id.value && !ui.valid.id(id.value)) return err("id", "Empieza por letra; 2-32 caracteres a-z, 0-9 y guiones.");
      if (!ui.valid.repo(repo.value.trim())) return err("repo", "Tiene que ser https://github.com/propietario/repositorio.");
      if (!branch.value.trim() || !ui.valid.branch(branch.value.trim())) return err("branch", "Rama no válida.");
      if (service.value && !/^[a-z0-9][a-z0-9_.-]*$/.test(service.value)) return err("service", "Minúsculas, números, punto, guion y guion bajo.");
      if (!ui.valid.url(url.value.trim())) return err("url", "Tiene que empezar por https://");
      const body = {
        name: name.value.trim(), repo: repo.value.trim(), branch: branch.value.trim(), description: desc.value.trim(),
        service: service.value.trim(), url: url.value.trim(),
      };
      await ui.busy(submit, async () => {
        try {
          let r;
          if (p) r = await updateProject(p, body);
          else {
            if (id && id.value) body.id = id.value;
            body.notes = "";
            r = await O.api.post("/api/projects", body);
            O.store.sync();
          }
          O.toast(p ? "Proyecto guardado." : "Proyecto creado.", { kind: "ok" });
          if (onSaved) onSaved(r);
        } catch (ex) {
          ui.fieldError(form, ex);
        }
      });
      return undefined;
    });
    return form;
  }

  // ------------------------------------------------------------- memory --

  function memoryLedger(p, state) {
    const all = p.memory || [];
    const active = all.filter((m) => m.active).sort((a, b) => (u.ms(b.created) || 0) - (u.ms(a.created) || 0));
    const archived = all.filter((m) => !m.active);
    const max = S.snap.settings.max_lessons_in_prompt || 12;
    const mem = async (body, btn, okText) => ui.busy(btn, async () => {
      await O.api.post(P(p.id, "memory"), body);
      O.toast(okText, { kind: "ok", timeout: 2500 });
      state.editing = null;
      O.store.sync();
    }, "No se pudo actualizar la memoria");

    const addText = ui.textarea({ rows: 2, placeholder: "Una lección concreta y reutilizable sobre este proyecto (p. ej. «los tests de integración necesitan make up antes»)" });
    const addForm = h("form", {
      class: "memory-add",
      on: {
        submit: (e) => {
          e.preventDefault();
          const t = addText.value.trim();
          if (!t) return;
          if (u.runes(t) > 600) { O.toast("Máximo 600 caracteres.", { kind: "warn" }); return; }
          if (all.length >= 200) { O.toast("La memoria admite 200 entradas: archiva o borra alguna.", { kind: "warn" }); return; }
          mem({ action: "add", text: t }, e.submitter || null, "Lección añadida a la memoria.");
        },
      },
    }, ui.field("Nueva lección", addText, { counter: ui.counter(addText, 600, "runes") }), ui.btn("Añadir", { type: "submit", size: "sm", kind: "primary", icon: "plus" }));

    const item = (m, idx) => {
      if (state.editing === m.id) {
        const ta = ui.textarea({ rows: 3 });
        ta.value = m.text;
        return h("li", { class: "memory-item is-editing" },
          ui.field("Editar lección", ta, { counter: ui.counter(ta, 600, "runes") }),
          ui.invisibleNote(ta),
          h("div", { class: "row-actions" },
            ui.btn("Guardar", { size: "sm", kind: "primary", onClick: (e) => mem({ action: "edit", id: m.id, text: ta.value.trim() }, e.currentTarget, "Lección actualizada.") }),
            ui.btn("Cancelar", { size: "sm", kind: "ghost", onClick: () => { state.editing = null; state.redraw(); } })));
      }
      return h("li", { class: ["memory-item", m.active && idx < max ? "is-injected" : null] },
        h("p", { class: "memory-text", text: O.visible(m.text) }),
        h("p", { class: "muted small" },
          ui.time(m.created),
          m.source_job ? [" · de ", h("a", { href: "#/trabajo/" + O.enc(m.source_job), class: "mono", text: m.source_job })] : " · añadida a mano",
          m.active && idx < max ? " · se inyecta en los prompts" : m.active ? " · fuera del límite de " + max : ""),
        h("div", { class: "row-actions" }, m.active
          ? [ui.btn("Editar", { size: "sm", kind: "ghost", icon: "edit", onClick: () => { state.editing = m.id; state.redraw(); } }),
            ui.btn("Archivar", { size: "sm", kind: "ghost", icon: "archive", onClick: (e) => mem({ action: "archive", id: m.id }, e.currentTarget, "Lección archivada: deja de inyectarse.") })]
          : [ui.btn("Restaurar", { size: "sm", kind: "ghost", icon: "undo", onClick: (e) => mem({ action: "restore", id: m.id }, e.currentTarget, "Lección restaurada.") }),
            ui.btn("Borrar", {
              size: "sm", kind: "danger-ghost", icon: "trash",
              onClick: async (e) => {
                const btn = e.currentTarget;
                if (await O.confirm({ title: "¿Borrar esta lección?", text: "Se borra para siempre (archivar la conserva sin usarla).", confirmLabel: "Borrar", danger: true })) {
                  mem({ action: "delete", id: m.id }, btn, "Lección borrada.");
                }
              },
            })]));
    };
    return h("div", null,
      h("p", { class: "muted", text: "Lo que el equipo ha aprendido de este proyecto. Las " + max + " lecciones activas más recientes se añaden a cada encargo. Llegan aprobando propuestas de la Bandeja o escribiéndolas aquí. " + all.length + " / 200." }),
      addForm,
      active.length ? h("ol", { class: "memory-list" }, active.map(item)) : ui.empty({ cls: "empty-sm", icon: "🧠", text: "Aún no hay lecciones activas. Cuando un agente termina un trabajo propone hasta 3; aparecerán en la Bandeja." }),
      archived.length ? h("details", { class: "memory-archived" },
        h("summary", null, "Archivadas (" + archived.length + ")"),
        h("ol", { class: "memory-list" }, archived.map(item))) : null);
  }

  // -------------------------------------------------------------- GitHub --

  function githubSection(p) {
    const box = h("div");
    const c = S.snap.credentials;
    if (!O.sel.githubReady(p.id)) {
      O.put(box, ui.empty({
        cls: "empty-sm", icon: "🔑",
        text: [c.github
          ? "El token de GitHub de la Oficina no cubre a este propietario (" + O.ownersText(c.github_owners) + ")."
          : "La Oficina no tiene token de GitHub: sin él no se ven issues ni PRs, no se crean PRs ni se comenta.",
        h("span", null, "Para añadirlo, en el servidor: ", h("code", { text: "sudo ./scripts/ax-web-bootstrap.sh office" }))],
      }));
      return box;
    }
    const load = (force) => {
      O.put(box, ui.loading("Leyendo GitHub…"));
      O.github(p.id, force).then((d) => {
        if (!d.enabled) {
          O.put(box, h("p", { class: "muted", text: "GitHub no está disponible para este proyecto." }));
          return;
        }
        const issueRow = (x) => h("li", { class: "gh-row" },
          h("div", { class: "gh-row-main" },
            h("a", { href: x.html_url, target: "_blank", rel: "noopener noreferrer", class: "gh-num mono", text: "#" + x.number }),
            h("span", { class: "gh-title", text: x.title }),
            O.ghLabels(x),
            h("span", { class: "muted small" }, O.ghUser(x) ? "@" + O.ghUser(x) + " · " : "", ui.time(x.created_at))),
          ui.menu("Asignar a…", [
            { label: "Un agente (trabajo)", icon: "🧑‍💻", href: "#/nuevo?proyecto=" + O.enc(p.id) + "&issue=" + x.number },
            { label: "Un equipo: plan → código → revisión", icon: "🧑‍🤝‍🧑", href: "#/nuevo?tab=equipo&plantilla=equipo&proyecto=" + O.enc(p.id) + "&issue=" + x.number },
          ], { kind: "ghost" }));
        const prRow = (x) => h("li", { class: "gh-row" },
          h("div", { class: "gh-row-main" },
            h("a", { href: x.html_url, target: "_blank", rel: "noopener noreferrer", class: "gh-num mono", text: "PR #" + x.number }),
            h("span", { class: "gh-title", text: x.title }),
            x.draft ? h("span", { class: "chip", text: "borrador" }) : null,
            h("span", { class: "muted small mono", text: O.ghRef(x.head) + " → " + O.ghRef(x.base) + (O.ghUser(x) ? " · @" + O.ghUser(x) : "") })),
          ui.menu("Revisar con…", [
            { label: "Un agente (revisión)", icon: "🔍", href: "#/nuevo?proyecto=" + O.enc(p.id) + "&tipo=revision&pr=" + x.number },
            { label: "El panel de revisión", icon: "🧑‍⚖️", href: "#/nuevo?tab=equipo&plantilla=panel&proyecto=" + O.enc(p.id) + "&pr=" + x.number },
          ], { kind: "ghost" }));
        O.put(box,
          h("div", { class: "section-actions" }, ui.btn("Actualizar", { size: "sm", kind: "ghost", icon: "refresh", onClick: () => load(true) })),
          h("h4", { class: "gh-h", text: "Issues abiertas (" + d.issues.length + ")" }),
          d.issues.length ? h("ul", { class: "gh-rows" }, d.issues.map(issueRow)) : h("p", { class: "muted", text: "No hay issues abiertas." }),
          h("h4", { class: "gh-h", text: "Pull requests abiertos (" + d.pulls.length + ")" }),
          d.pulls.length ? h("ul", { class: "gh-rows" }, d.pulls.map(prRow)) : h("p", { class: "muted", text: "No hay PRs abiertos." }));
      }).catch((e) => O.put(box, h("p", { class: "field-error", text: "No se pudo leer GitHub: " + e.message }), ui.btn("Reintentar", { size: "sm", onClick: () => load(true) })));
    };
    load(false);
    return box;
  }

  // -------------------------------------------------------------- detail --

  O.route("/proyectos/:id", {
    title: "Proyecto",
    mount(root, params) {
      if (params.id === "nuevo") {
        O.put(root,
          ui.pageHead({ back: { href: "#/proyectos", label: "Proyectos" }, title: "Nuevo proyecto", icon: "📁", subtitle: "Un repositorio público de GitHub. Los agentes trabajan sobre una copia efímera en el sandbox." }),
          h("div", { class: "card" }, projectForm(null, (r) => O.go(r && r.id ? "#/proyectos/" + O.enc(r.id) : "#/proyectos"))));
        return {};
      }
      const id = params.id;
      if (!O.sel.project(id)) {
        O.put(root, ui.empty({ icon: "🔎", title: "Este proyecto no existe", actions: ui.btn("Ver proyectos", { href: "#/proyectos", kind: "primary" }) }));
        return {};
      }
      const head = h("div");
      const infoBox = h("div", { class: "card" });
      const notesBox = h("div", { class: "card" });
      const memBox = h("div");
      const ghBox = h("div");
      const side = h("div", { class: "side-col" });
      const memState = { editing: null, redraw: () => { memBox._sig = null; render(); } };
      let notesDirty = false;
      O.put(root, head, h("div", { class: "two-col" },
        h("div", { class: "main-col" },
          ui.section("Datos", { icon: "🗂️" }, infoBox),
          ui.section("Notas para los agentes", { icon: "📝", sub: "Se añaden a cada encargo sobre este proyecto: convenciones, cómo probar, qué no tocar." }, notesBox),
          ui.section("Memoria", { icon: "🧠" }, memBox),
          ui.section("GitHub", { icon: "🐙" }, ghBox)),
        side));
      O.put(ghBox, githubSection(O.sel.project(id)));

      const render = () => {
        const p = O.sel.project(id);
        if (!p) {
          O.put(root, ui.empty({ icon: "👋", title: "El proyecto ya no existe", actions: ui.btn("Ver proyectos", { href: "#/proyectos", kind: "primary" }) }));
          return;
        }
        O.router.setTitle(p.name);
        ui.memo(head, JSON.stringify([p.name, p.seeded, p.archived, p.service, p.repo]), () => ui.pageHead({
          back: { href: "#/proyectos", label: "Proyectos" },
          title: p.name, icon: "📁",
          subtitle: h("span", null, repoLink(p), " ", badges(p)),
        }));
        ui.memo(infoBox, JSON.stringify([p.name, p.repo, p.branch, p.description, p.service, p.url, p.seeded]), () => projectForm(p, () => {}));
        if (!notesDirty) {
          ui.memo(notesBox, p.notes || "", () => {
            const ta = ui.textarea({ rows: 6, placeholder: "p. ej. «Usa pnpm, no npm. Los tests: make test. No toques la carpeta legacy/.»" });
            ta.value = p.notes || "";
            ta.addEventListener("input", () => { notesDirty = true; });
            return [ui.field("Notas", ta, { field: "notes", counter: ui.counter(ta, 8192) }),
              h("div", { class: "row-actions" }, ui.btn("Guardar notas", {
                size: "sm", kind: "primary", icon: "check",
                onClick: async (e) => {
                  if (u.bytes(ta.value) > 8192) { O.toast("Las notas superan 8 KiB.", { kind: "warn" }); return; }
                  await ui.busy(e.currentTarget, async () => {
                    await updateProject(O.sel.project(id), { notes: ta.value });
                    notesDirty = false;
                    O.toast("Notas guardadas: irán en los próximos encargos.", { kind: "ok" });
                  }, "No se pudieron guardar las notas");
                },
              }))];
          });
        }
        ui.memo(memBox, JSON.stringify([p.memory, memState.editing, S.snap.settings.max_lessons_in_prompt]), () => memoryLedger(p, memState));
        const jobs = O.sel.jobs().filter((j) => j.project_id === id).slice(0, 8);
        ui.memo(side, JSON.stringify([p.archived, p.seeded, jobs.map((j) => j.id + j.status)]), () => [
          h("div", { class: "card" },
            h("h3", { class: "card-title", text: "Acciones" }),
            h("div", { class: "stack" },
              p.archived ? null : ui.btn("Encargar trabajo", { href: "#/nuevo?proyecto=" + O.enc(id), kind: "primary", icon: "plus" }),
              p.archived ? null : ui.btn("Reunir un equipo", { href: "#/nuevo?tab=equipo&proyecto=" + O.enc(id), icon: "team" }),
              ui.btn("Ver sus trabajos", { href: "#/tablero?proyecto=" + O.enc(id), icon: "board" }),
              ui.btn(p.archived ? "Restaurar" : "Archivar", {
                icon: p.archived ? "undo" : "archive",
                onClick: async (e) => {
                  const btn = e.currentTarget;
                  if (!p.archived && !(await O.confirm({ title: "¿Archivar " + p.name + "?", text: "No aparecerá al encargar trabajo. Su memoria y sus trabajos se conservan.", confirmLabel: "Archivar" }))) return;
                  await ui.busy(btn, async () => {
                    await updateProject(p, { archived: !p.archived });
                    O.toast(p.archived ? "Proyecto restaurado." : "Proyecto archivado.", { kind: "ok" });
                  }, "No se pudo cambiar");
                },
              }),
              p.seeded ? null : ui.btn("Borrar proyecto", {
                icon: "trash", kind: "danger-ghost",
                onClick: async (e) => {
                  const btn = e.currentTarget;
                  const ok = await O.confirm({ title: "¿Borrar " + p.name + "?", text: "Se borran el proyecto y su memoria. Los trabajos pasados se conservan.", confirmLabel: "Borrar", danger: true, typeToConfirm: p.id });
                  if (!ok) return;
                  await ui.busy(btn, async () => {
                    await O.api.post(P(id, "delete"), {});
                    O.toast("Proyecto borrado.", { kind: "ok" });
                    O.store.sync();
                    O.go("#/proyectos");
                  }, "No se pudo borrar");
                },
              }))),
          h("div", { class: "card" },
            h("h3", { class: "card-title", text: "Últimos trabajos" }),
            jobs.length ? h("div", { class: "card-list" }, jobs.map((j) => ui.jobCard(j))) : h("p", { class: "muted", text: "Aún no hay trabajos en este proyecto." })),
        ]);
      };
      render();
      return { update: render };
    },
  });

})();
