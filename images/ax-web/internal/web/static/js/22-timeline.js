/*
 * Oficina de agentes · live timeline of a job (harness events).
 *
 * timeline({jobId, compact}) renders the events of the watched job as they
 * arrive on the stream (batched per animation frame), with an icon per
 * kind and tool, tool results folded under their call, filters and a
 * "seguir" toggle that keeps the view pinned to the newest event.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, fmt, u, ui } = O;

  const TOOL_ICONS = {
    Bash: "💻", Read: "📖", Edit: "✏️", MultiEdit: "✏️", Write: "📝", NotebookEdit: "📓",
    Grep: "🔎", Glob: "🗂️", WebFetch: "🌐", WebSearch: "🌍", Task: "🤝", Agent: "🤝", TodoWrite: "📋",
  };
  const KIND = {
    system: { icon: "⚙️", label: "Sistema", group: "sys" },
    init: { icon: "🚀", label: "Inicio", group: "sys" },
    text: { icon: "💬", label: "Mensaje", group: "msg" },
    thinking: { icon: "🤔", label: "Razonamiento", group: "msg" },
    tool: { icon: "🔧", label: "Herramienta", group: "tool" },
    tool_result: { icon: "↳", label: "Resultado", group: "tool" },
    todo: { icon: "📋", label: "Plan", group: "msg" },
    usage: { icon: "📊", label: "Consumo", group: "sys" },
    result: { icon: "🏁", label: "Resultado final", group: "msg" },
    stderr: { icon: "⚠️", label: "stderr", group: "err" },
    error: { icon: "⛔", label: "Error", group: "err" },
  };
  const toolIcon = (t) => TOOL_ICONS[t] || (t && t.includes("/") ? "🔌" : "🔧");

  function usageChips(ev) {
    const out = [];
    if (ev.cost_usd) out.push(h("span", { class: "chip", text: fmt.usd(ev.cost_usd) }));
    if (ev.in || ev.out) out.push(h("span", { class: "chip", text: fmt.tokens(ev.in || 0) + " entrada · " + fmt.tokens(ev.out || 0) + " salida" }));
    if (ev.cache_r || ev.cache_w) out.push(h("span", { class: "chip", text: "caché " + fmt.tokens(ev.cache_r || 0) + " leída · " + fmt.tokens(ev.cache_w || 0) + " escrita" }));
    if (ev.turns) out.push(h("span", { class: "chip", text: ev.turns + " " + u.plural(ev.turns, "turno", "turnos") }));
    if (ev.ms) out.push(h("span", { class: "chip", text: fmt.duration(ev.ms) }));
    if (ev.model) out.push(h("span", { class: "chip mono", text: ev.model }));
    return out;
  }

  // Long text folds behind "ver más".
  function foldable(node, len, limit) {
    if (len <= limit) return node;
    const wrap = h("div", { class: "fold is-folded" }, node);
    const btn = h("button", {
      type: "button", class: "link-btn",
      on: {
        click: () => {
          const folded = wrap.classList.toggle("is-folded");
          btn.textContent = folded ? "Ver más" : "Ver menos";
        },
      },
    }, "Ver más");
    return h("div", null, wrap, btn);
  }

  // dup: the result repeats the previous assistant message word for word
  // (Claude's final "result" event does), so only its usage is shown.
  function body(ev, dup) {
    const text = ev.text || "";
    switch (ev.k) {
      case "text":
        return foldable(text.length < 12000 ? O.md.render(text, "md-compact") : h("p", { class: "pre-wrap", text }), text.length, 900);
      case "thinking":
        return h("details", { class: "tl-think" }, h("summary", { text: "Razonamiento" }), h("p", { class: "pre-wrap", text }));
      case "tool":
        return h("div", { class: "tl-tool" },
          h("strong", { class: "tl-tool-name", text: ev.tool || "herramienta" }),
          ev.input ? h("code", { class: "tl-tool-input mono", text: ev.input }) : null);
      case "tool_result":
        return h("details", { class: ["tl-res", ev.ok === false ? "is-bad" : "is-ok"] },
          h("summary", { text: ev.ok === false ? "Falló" : "Resultado" }),
          h("pre", { class: "pre mono", text: text || "(sin salida)" }));
      case "todo":
        return h("pre", { class: "tl-todo mono", text });
      case "usage":
        return h("div", { class: "chips" }, usageChips(ev));
      case "result":
        return h("div", { class: "tl-result" },
          h("strong", { class: "tl-result-label", text: "Resultado final" }),
          h("div", { class: "chips" }, usageChips(ev)),
          dup ? h("p", { class: "muted small", text: "El texto del resultado es el último mensaje, justo encima." })
            : text ? foldable(O.md.render(text, "md-compact"), text.length, 1200) : null);
      case "init":
        return h("div", null, h("span", { text }), ev.model ? h("span", { class: "chip mono", text: ev.model }) : null);
      case "stderr":
        return h("code", { class: "tl-stderr mono", text });
      default:
        return h("p", { class: "pre-wrap", text: text || (ev.tool ? ev.tool + " " + (ev.input || "") : "") });
    }
  }

  O.timeline = function timeline(o) {
    const opts = o || {};
    const jobId = opts.jobId;
    const compact = !!opts.compact;
    const maxRows = compact ? 60 : 4000;
    let follow = true;
    let pending = [];
    let raf = 0;
    let rows = 0;
    let lastTool = null;
    // Trimmed text of the newest "text" event, to spot a repeated result.
    let lastText = "";
    let firstSeq = 0;
    let lastSeq = 0;
    let unseen = 0;
    let programmatic = false;

    const list = h("div", { class: ["tl-list", compact && "is-compact"], role: "log", "aria-live": "off", "aria-label": "Registro en directo" });
    const emptyNote = h("p", { class: "tl-empty muted", text: opts.emptyText || "Aún no hay eventos. Aparecerán aquí en cuanto el agente empiece." });
    list.appendChild(emptyNote);
    const counter = h("span", { class: "muted tl-count" });
    const followBtn = h("button", {
      type: "button", class: "btn btn-sm btn-ghost tl-follow", "aria-pressed": "true",
      on: { click: () => setFollow(!follow) },
    }, O.icon("chevronDown", { size: 15 }), h("span", { text: "Seguir" }));
    const jumpBtn = h("button", {
      type: "button", class: "btn btn-sm btn-primary tl-jump", hidden: true,
      on: { click: () => setFollow(true) },
    }, "Nuevos eventos ↓");
    const olderBtn = h("button", {
      type: "button", class: "btn btn-sm btn-ghost", hidden: true,
      on: { click: () => loadOlder() },
    }, "Cargar eventos anteriores");
    const filter = ui.select([
      { value: "all", label: "Todo" },
      { value: "msg", label: "Mensajes" },
      { value: "tool", label: "Herramientas" },
      { value: "err", label: "Problemas" },
    ], "all", { "aria-label": "Filtrar eventos", class: "input select select-sm" });
    filter.addEventListener("change", () => { list.dataset.filter = filter.value; });

    const toolbar = h("div", { class: "tl-toolbar" },
      counter, compact ? null : filter, compact ? null : olderBtn, h("span", { class: "spacer" }), jumpBtn, followBtn);
    const el = h("div", { class: ["timeline", compact && "is-compact"] }, toolbar, list);

    function setFollow(on) {
      follow = on;
      followBtn.setAttribute("aria-pressed", on ? "true" : "false");
      followBtn.classList.toggle("is-on", on);
      if (on) {
        unseen = 0;
        jumpBtn.hidden = true;
        scrollToEnd();
      }
    }
    function scrollToEnd() {
      programmatic = true;
      list.scrollTop = list.scrollHeight;
      requestAnimationFrame(() => { programmatic = false; });
    }
    list.addEventListener("scroll", () => {
      if (programmatic) return;
      const atEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
      if (atEnd && !follow) setFollow(true);
      else if (!atEnd && follow) {
        follow = false;
        followBtn.setAttribute("aria-pressed", "false");
        followBtn.classList.remove("is-on");
      }
    });

    function row(ev, dup) {
      const k = KIND[ev.k] || { icon: "•", label: ev.k, group: "sys" };
      const icon = ev.k === "tool" ? toolIcon(ev.tool) : ev.k === "tool_result" ? (ev.ok === false ? "❌" : "✅") : k.icon;
      return h("div", { class: ["tl-row", "k-" + ev.k, "g-" + k.group], dataset: { seq: String(ev.seq || 0) } },
        h("span", { class: "tl-icon", "aria-hidden": "true", text: icon }),
        h("span", { class: "tl-time mono", title: fmt.full(ev.t), text: fmt.timeSec(ev.t) }),
        h("div", { class: "tl-body" }, h("span", { class: "sr-only", text: k.label + ": " }), body(ev, dup)));
    }

    function build(events) {
      const frag = document.createDocumentFragment();
      for (const ev of events) {
        if (ev.k === "tool_result" && lastTool && !lastTool.dataset.res) {
          // Fold the result under its call.
          lastTool.dataset.res = "1";
          lastTool.classList.add(ev.ok === false ? "is-bad" : "is-ok");
          lastTool.querySelector(".tl-body").appendChild(body(ev));
          const ic = lastTool.querySelector(".tl-icon");
          if (ev.ok === false) ic.textContent = "❌";
          continue;
        }
        let dup = false;
        if (ev.k === "text") lastText = String(ev.text || "").trim();
        else if (ev.k === "result") {
          const t = String(ev.text || "").trim();
          dup = t !== "" && t === lastText;
        }
        const r = row(ev, dup);
        if (ev.k === "tool") lastTool = r;
        else if (ev.k !== "tool_result") lastTool = null;
        frag.appendChild(r);
        rows++;
      }
      return frag;
    }

    function flush() {
      raf = 0;
      if (!pending.length) return;
      const batch = pending;
      pending = [];
      if (emptyNote.parentNode) emptyNote.remove();
      list.appendChild(build(batch));
      while (rows > maxRows && list.firstElementChild) {
        list.firstElementChild.remove();
        rows--;
      }
      counter.textContent = fmt.num(lastSeq || rows) + " " + u.plural(lastSeq || rows, "evento", "eventos");
      if (follow) scrollToEnd();
      else {
        unseen += batch.length;
        jumpBtn.hidden = false;
        jumpBtn.textContent = unseen + " nuevos ↓";
      }
    }

    function accept(ev) {
      if (typeof ev.seq === "number" && ev.seq > 0) {
        if (ev.seq <= lastSeq) return;
        if (!firstSeq) firstSeq = ev.seq;
        lastSeq = ev.seq;
      }
      pending.push(ev);
      if (!raf) raf = requestAnimationFrame(flush);
      if (!compact && firstSeq > 1) olderBtn.hidden = false;
    }

    async function loadOlder() {
      olderBtn.disabled = true;
      try {
        const older = [];
        let after = 0;
        for (let guard = 0; guard < 10; guard++) {
          const d = await O.api.get("/api/jobs/" + O.enc(jobId) + "/events?after=" + after);
          const evs = Array.isArray(d.events) ? d.events : [];
          for (const ev of evs) if (ev.seq < firstSeq) older.push(ev);
          const last = evs.length ? evs[evs.length - 1].seq : 0;
          if (!d.more || !evs.length || last >= firstSeq - 1) break;
          after = last;
        }
        if (older.length) {
          const prevTool = lastTool, prevText = lastText;
          lastTool = null;
          lastText = "";
          const frag = build(older);
          lastTool = prevTool;
          lastText = prevText;
          list.insertBefore(frag, list.firstElementChild);
          firstSeq = older[0].seq;
        }
        olderBtn.hidden = true;
      } catch (e) {
        O.fail(e, "No se pudieron cargar los eventos anteriores");
        olderBtn.disabled = false;
      }
    }

    for (const ev of O.stream.events(jobId)) accept(ev);
    const off = O.on("log", (m) => { if (m.jobId === jobId) accept(m.ev); });
    setFollow(true);

    return {
      el,
      count: () => lastSeq || rows,
      destroy() {
        off();
        if (raf) cancelAnimationFrame(raf);
      },
    };
  };
})();
