/*
 * Oficina de agentes · the office floor (#/).
 *
 * An illustrated SVG office: one desk per enabled agent (emoji avatar in
 * the agent's colour, name plate, a monitor whose glow tells the status,
 * idle animations and a speech bubble with the live activity), the server
 * room with the single AX sandbox slot and the queue, the meeting room
 * with the running team, and a whiteboard with today's counters. The
 * scene is built once per layout and then patched in place, so the CSS
 * animations keep running while deltas arrive.
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const { h, s, u, fmt, ui, L } = O;
  const S = O.state;

  const DESK_W = 240, DESK_H = 206;
  const WIDE_MIN = 860;

  // ------------------------------------------------------------- helpers --

  function wrap(text, max, lines) {
    const words = String(text || "").replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
    const out = [];
    let cur = "";
    for (let w of words) {
      while (Array.from(w).length > max) {
        if (cur) { out.push(cur); cur = ""; }
        const a = Array.from(w);
        out.push(a.slice(0, max).join(""));
        w = a.slice(max).join("");
      }
      if (!cur) cur = w;
      else if (Array.from(cur + " " + w).length <= max) cur += " " + w;
      else { out.push(cur); cur = w; }
    }
    if (cur) out.push(cur);
    if (out.length > lines) {
      const keep = out.slice(0, lines);
      keep[lines - 1] = u.trunc(keep[lines - 1] + " " + out[lines], max);
      if (!keep[lines - 1].endsWith("…")) keep[lines - 1] = u.trunc(keep[lines - 1], max - 1) + "…";
      return keep;
    }
    return out;
  }

  const textW = (str, px) => Array.from(str).length * px * 0.58 + 18;

  function multiText(x, y, lines, lh, attrs) {
    return s("text", Object.assign({ x, y }, attrs),
      lines.map((l, i) => s("tspan", { x, dy: i === 0 ? 0 : lh, text: l })));
  }

  function skyClass() {
    const hr = new Date(O.now()).getHours();
    if (hr >= 8 && hr < 18) return "sky-day";
    if ((hr >= 18 && hr < 21) || (hr >= 6 && hr < 8)) return "sky-dusk";
    return "sky-night";
  }

  const STATE_TEXT = {
    working: "Trabajando", attention: "Te necesita", queued: "En cola",
    idle: "Libre", sleeping: "Descansando",
  };

  function deskState(agent) {
    const st = O.sel.agentState(agent.id);
    let chip = O.own(STATE_TEXT, st.state);
    let bubble = "";
    if (st.state === "working") {
      const j = st.job;
      if (j.status === "preparando") { chip = "Preparando"; bubble = "Preparando el sandbox…"; }
      else if (j.status === "limpiando") { chip = "Recogiendo"; bubble = "Recogiendo y guardando los cambios…"; }
      else if (j.stalled) bubble = "😶 Sin noticias desde hace un rato…";
      else {
        const act = S.snap.active && S.snap.active.job_id === j.id ? S.snap.active.activity : "";
        bubble = j.activity || act || "Pensando…";
      }
    }
    if (st.state === "queued") chip = "En cola · " + st.queued.length;
    return { st, chip, bubble };
  }

  // ---------------------------------------------------------------- layout --

  function layout(n, wide) {
    const count = Math.max(1, n);
    if (wide) {
      const cols = count <= 9 ? 3 : 4;
      const scale = cols === 3 ? 1 : 0.8;
      const floorX = 24, floorW = 852;
      const cellW = (floorW - 32) / cols;
      const cellH = DESK_H * scale + 46;
      const deskTop = 226;
      const rows = Math.ceil(count / cols);
      const floorH = Math.max(870, deskTop + rows * cellH + 40) - 24;
      const H = floorH + 48;
      const desks = [];
      for (let i = 0; i < n; i++) {
        const c = i % cols, r = Math.floor(i / cols);
        desks.push({ x: floorX + 16 + c * cellW + (cellW - DESK_W * scale) / 2, y: deskTop + r * cellH, scale });
      }
      return {
        wide, W: 1280, H, desks, deskTop, rows, cellH,
        floor: { x: floorX, y: 24, w: floorW, h: floorH },
        board: { x: 52, y: 50, w: 420, h: 128 },
        server: { x: 900, y: 24, w: 356, h: 410 },
        meeting: { x: 900, y: 454, w: 356, h: H - 454 - 24 },
      };
    }
    const scale = 0.76, cols = 2;
    const cellW = 194, cellH = DESK_H * scale + 44;
    const rows = Math.ceil(count / cols);
    const board = { x: 16, y: 16, w: 388, h: 122 };
    const server = { x: 16, y: 154, w: 388, h: 280 };
    const floor = { x: 16, y: 450, w: 388, h: rows * cellH + 96 };
    const deskTop = floor.y + 104;
    const desks = [];
    for (let i = 0; i < n; i++) {
      const c = i % cols, r = Math.floor(i / cols);
      desks.push({ x: floor.x + 6 + c * cellW + (cellW - DESK_W * scale) / 2, y: deskTop + r * cellH, scale });
    }
    const meeting = { x: 16, y: floor.y + floor.h + 16, w: 388, h: 290 };
    return { wide, W: 420, H: meeting.y + meeting.h + 16, desks, deskTop, rows, cellH, floor, board, server, meeting };
  }

  // ------------------------------------------------------------------ defs --

  function defs() {
    return s("defs", null,
      s("pattern", { id: "of-wood", width: 120, height: 28, patternUnits: "userSpaceOnUse" },
        s("rect", { width: 120, height: 28, class: "of-wood-a" }),
        s("rect", { y: 14, width: 120, height: 14, class: "of-wood-b" }),
        s("path", { d: "M0 .5H120M0 14.5H120M38 0v14M92 14v14", class: "of-wood-line" })),
      s("pattern", { id: "of-tiles", width: 32, height: 32, patternUnits: "userSpaceOnUse" },
        s("rect", { width: 32, height: 32, class: "of-tile" }),
        s("path", { d: "M0 .5H32M.5 0V32", class: "of-tile-line" })),
      s("pattern", { id: "of-carpet", width: 10, height: 10, patternUnits: "userSpaceOnUse" },
        s("rect", { width: 10, height: 10, class: "of-carpet" }),
        s("circle", { cx: 5, cy: 5, r: .9, class: "of-carpet-dot" })),
      s("filter", { id: "of-blur", x: "-50%", y: "-50%", width: "200%", height: "200%" },
        s("feGaussianBlur", { stdDeviation: 7 })),
      s("filter", { id: "of-soft", x: "-20%", y: "-20%", width: "140%", height: "140%" },
        s("feDropShadow", { dx: 0, dy: 2, stdDeviation: 2.2, class: "of-shadow" })),
      s("clipPath", { id: "of-screen-clip" }, s("rect", { x: 28, y: 44, width: 54, height: 34, rx: 3 })));
  }

  // ---------------------------------------------------------------- pieces --

  function plant(x, y, size) {
    const k = size || 1;
    return s("g", { class: "plant", transform: `translate(${x} ${y}) scale(${k})` },
      s("ellipse", { cx: 0, cy: 22, rx: 16, ry: 5, class: "plant-shadow" }),
      s("path", { d: "M-12 6h24l-3 16h-18z", class: "plant-pot" }),
      s("circle", { cx: -8, cy: -2, r: 10, class: "plant-leaf" }),
      s("circle", { cx: 8, cy: -4, r: 11, class: "plant-leaf alt" }),
      s("circle", { cx: 0, cy: -14, r: 11, class: "plant-leaf" }),
      s("circle", { cx: -2, cy: 2, r: 8, class: "plant-leaf alt" }));
  }

  function deskDecor(kind) {
    if (kind === 0) {
      return s("g", { transform: "translate(206 88)" },
        s("path", { d: "M-6 0h12l-2 8h-8z", class: "plant-pot" }),
        s("circle", { cx: -3, cy: -3, r: 4.5, class: "plant-leaf" }),
        s("circle", { cx: 3, cy: -4, r: 4.5, class: "plant-leaf alt" }));
    }
    if (kind === 1) {
      return s("g", { transform: "translate(196 84)" },
        s("rect", { x: 0, y: 6, width: 22, height: 5, rx: 1, class: "book b1" }),
        s("rect", { x: 2, y: 1, width: 19, height: 5, rx: 1, class: "book b2" }),
        s("rect", { x: 1, y: -4, width: 20, height: 5, rx: 1, class: "book b3" }));
    }
    return s("g", { transform: "translate(208 74)" },
      s("path", { d: "M0 22v-14l-8-6", class: "lamp-arm" }),
      s("path", { d: "M-14 -2l10-6 4 7z", class: "lamp-head" }),
      s("rect", { x: -6, y: 21, width: 12, height: 3, rx: 1.5, class: "lamp-base" }));
  }

  function buildDesk(agent, idx, pos, narrow) {
    const color = u.color(agent.color);
    const keys = [];
    for (let r = 0; r < 2; r++) {
      for (let c = 0; c < 6; c++) keys.push(s("rect", { x: 103 + c * 6, y: 100.5 + r * 4.5, width: 4, height: 2.6, rx: .6, class: "key" }));
    }
    const lines = [];
    for (let i = 0; i < 9; i++) {
      lines.push(s("rect", { x: 32 + (i % 3) * 3, y: 47 + i * 5, width: 18 + ((i * 7) % 24), height: 2.2, rx: 1, class: "mon-line" }));
    }
    const chipRect = s("rect", { x: 0, y: -11, width: 80, height: 22, rx: 11, class: "chip-bg" });
    const chipText = s("text", { x: 0, y: 4, class: "chip-text" });
    const chip = s("g", { class: "desk-chip", transform: "translate(234 18)" }, chipRect, chipText);
    const ticketText = s("text", { x: 16, y: 15, class: "ticket-text", "text-anchor": "middle" });
    const ticket = s("g", { class: "desk-ticket", transform: "translate(10 6)" },
      s("path", { d: "M0 3a3 3 0 0 1 3-3h26a3 3 0 0 1 3 3v5a3 3 0 0 0 0 6v5a3 3 0 0 1-3 3H3a3 3 0 0 1-3-3v-5a3 3 0 0 0 0-6z", class: "ticket-bg" }),
      ticketText);
    const title = s("title");
    const g = s("g", {
      class: "desk is-idle", tabindex: "0", role: "button",
      transform: `translate(${pos.x} ${pos.y}) scale(${pos.scale})`,
      style: { "--agent": color },
      dataset: { agent: agent.id },
    },
      title,
      s("rect", { x: 2, y: 2, width: DESK_W - 4, height: DESK_H - 4, rx: 18, class: "desk-hit" }),
      s("ellipse", { cx: 120, cy: 176, rx: 104, ry: 10, class: "desk-shadow" }),
      // chair and person
      s("rect", { x: 86, y: 30, width: 68, height: 62, rx: 18, class: "chair" }),
      s("g", { class: "desk-avatar" },
        s("circle", { cx: 120, cy: 60, r: 28, class: "avatar-bg" }),
        s("circle", { cx: 120, cy: 60, r: 28, class: "avatar-ring" }),
        s("text", { x: 120, y: 62, class: "avatar-emoji emoji", "text-anchor": "middle", "dominant-baseline": "central", text: agent.emoji || "🤖" })),
      // monitor
      s("ellipse", { cx: 55, cy: 62, rx: 44, ry: 32, class: "mon-glow", filter: "url(#of-blur)" }),
      s("rect", { x: 52, y: 80, width: 6, height: 14, class: "mon-stand" }),
      s("rect", { x: 42, y: 92, width: 26, height: 4, rx: 2, class: "mon-stand" }),
      s("rect", { x: 24, y: 40, width: 62, height: 42, rx: 5, class: "mon-bezel" }),
      s("rect", { x: 28, y: 44, width: 54, height: 34, rx: 3, class: "mon-screen" }),
      s("g", { "clip-path": "url(#of-screen-clip)" }, s("g", { class: "mon-lines" }, lines)),
      s("text", { x: 55, y: 66, class: "mon-sleep", "text-anchor": "middle", text: "☾" }),
      // desk
      s("rect", { x: 16, y: 92, width: 208, height: 30, rx: 7, class: "desk-top" }),
      s("rect", { x: 22, y: 121, width: 196, height: 34, rx: 5, class: "desk-front" }),
      s("rect", { x: 30, y: 154, width: 8, height: 16, rx: 2, class: "desk-leg" }),
      s("rect", { x: 202, y: 154, width: 8, height: 16, rx: 2, class: "desk-leg" }),
      s("rect", { x: 99, y: 97, width: 42, height: 14, rx: 2.5, class: "kbd" }),
      s("g", { class: "keys" }, keys),
      // mug with steam
      s("g", { class: "mug-g", transform: "translate(160 96)" },
        s("path", { d: "M3 -4c-3 -4 3 -6 0 -10M9 -4c-3 -4 3 -6 0 -10", class: "steam" }),
        s("rect", { x: 0, y: 0, width: 13, height: 13, rx: 3, class: "mug" }),
        s("path", { d: "M13 3h2.5a3 3 0 0 1 0 6H13", class: "mug-handle" })),
      deskDecor(idx % 3),
      // name plate
      s("rect", { x: 66, y: 127, width: 108, height: 22, rx: 5, class: "nameplate" }),
      s("text", { x: 120, y: 138.5, class: "nameplate-text", "text-anchor": "middle", "dominant-baseline": "central", text: u.trunc(agent.name, 14) }),
      s("text", { x: 120, y: 186, class: "desk-role", "text-anchor": "middle", text: u.trunc(agent.role || "", 30) }),
      s("text", { x: 120, y: 201, class: "desk-model", "text-anchor": "middle", text: u.trunc(ui.modelLabel(agent.harness, agent.model, agent.effort) + " · " + (agent.mode === "completo" ? "completo" : "lectura"), 36) }),
      // sleeping and attention marks
      s("g", { class: "zzz" },
        s("text", { x: 92, y: 34, class: "z z1", text: "z" }),
        s("text", { x: 82, y: 23, class: "z z2", text: "z" }),
        s("text", { x: 70, y: 12, class: "z z3", text: "Z" })),
      s("g", { class: "beacon-g", transform: "translate(90 22)" },
        s("circle", { r: 14, class: "beacon-halo" }),
        s("circle", { r: 9, class: "beacon" }),
        s("text", { y: 1, class: "beacon-text", "text-anchor": "middle", "dominant-baseline": "central", text: "!" })),
      chip, ticket);
    const bh = narrow ? 56 : 46;
    const bubbleText = s("text", { x: 30, y: -bh + 19, class: "bubble-text" });
    const bubble = s("g", { class: "bubble", transform: `translate(${pos.x} ${pos.y}) scale(${pos.scale})`, "aria-hidden": "true" },
      s("rect", { x: 14, y: -bh, width: 212, height: bh, rx: 14, class: "bubble-bg" }),
      s("path", { d: "M112 -1l8 12 7 -12z", class: "bubble-bg bubble-tail" }),
      bubbleText);
    return {
      g, bubble, bubbleText, chip, chipRect, chipText, ticket, ticketText, title,
      wrap: narrow ? 23 : 30, lh: narrow ? 19 : 16, chipPx: narrow ? 13.5 : 11.5,
    };
  }

  function updateDesk(refs, agent) {
    const { st, chip, bubble } = deskState(agent);
    refs.g.setAttribute("class", "desk is-" + st.state + (st.job && st.job.stalled ? " is-stalled" : ""));
    refs.chipText.textContent = chip;
    const w = textW(chip, refs.chipPx);
    refs.chipRect.setAttribute("width", w.toFixed(0));
    refs.chipRect.setAttribute("x", (-w).toFixed(0));
    refs.chipText.setAttribute("x", (-w / 2).toFixed(0));
    refs.chipText.setAttribute("text-anchor", "middle");
    const q = st.queued.length;
    refs.ticket.style.setProperty("display", q ? "inline" : "none");
    refs.ticketText.textContent = "🎫" + q;
    const lines = bubble ? wrap(bubble, refs.wrap, 2) : [];
    refs.bubble.style.setProperty("display", lines.length ? "inline" : "none");
    const sig = lines.join("\n");
    if (refs.bubbleSig !== sig) {
      refs.bubbleSig = sig;
      refs.bubbleText.replaceChildren(...lines.map((l, i) => s("tspan", { x: 30, dy: i === 0 ? (lines.length === 1 ? refs.lh / 2 : 0) : refs.lh, text: l })));
    }
    let label = agent.name + ", " + (agent.role || "") + ": " + chip;
    if (st.job) label += ". " + (st.job.title || "") + (bubble ? ". " + bubble : "");
    if (st.attention.length) label += ". " + st.attention.length + " " + u.plural(st.attention.length, "cosa pendiente", "cosas pendientes");
    refs.g.setAttribute("aria-label", label);
    refs.title.textContent = label;
  }

  // ---------------------------------------------------------- whiteboard --

  function whiteboard(r) {
    return s("a", { href: "#/tablero", class: "wb-link", "aria-label": "Pizarra: abrir el tablero" },
      s("rect", { x: r.x, y: r.y, width: r.w, height: r.h, rx: 8, class: "wb-frame", filter: "url(#of-soft)" }),
      s("rect", { x: r.x + 7, y: r.y + 7, width: r.w - 14, height: r.h - 22, rx: 4, class: "wb" }),
      s("rect", { x: r.x + r.w * 0.25, y: r.y + r.h - 12, width: r.w * 0.5, height: 7, rx: 3, class: "wb-tray" }),
      s("rect", { x: r.x + r.w * 0.29, y: r.y + r.h - 15, width: 18, height: 4, rx: 2, class: "marker m1" }),
      s("rect", { x: r.x + r.w * 0.29 + 24, y: r.y + r.h - 15, width: 18, height: 4, rx: 2, class: "marker m2" }),
      s("rect", { x: r.x + r.w * 0.29 + 48, y: r.y + r.h - 15, width: 18, height: 4, rx: 2, class: "marker m3" }),
      s("text", { x: r.x + 18, y: r.y + 28, class: "wb-title", text: "Pizarra · hoy" }),
      s("g", { class: "wb-values" }));
  }

  function fillWhiteboard(g, r) {
    const today = O.sel.today();
    const vals = [
      { label: "En cola", value: String(O.sel.queueJobs().length), cls: "m-blue" },
      { label: "En curso", value: String(O.sel.running().length), cls: "m-green" },
      { label: "Hechos hoy", value: String(today.done), cls: "m-ink" },
      { label: "Coste hoy", value: fmt.usd(today.cost).replace(/\s?US\$/, " $"), cls: "m-red", small: true },
    ];
    const colW = (r.w - 28) / 4;
    O.put(g, vals.map((v, i) => {
      const cx = r.x + 14 + colW * i + colW / 2;
      return s("g", null,
        s("text", { x: cx, y: r.y + 54, class: "wb-label", "text-anchor": "middle", text: v.label }),
        s("text", { x: cx, y: r.y + (v.small ? 90 : 94), class: ["wb-value", v.cls, v.small && "is-small"].filter(Boolean).join(" "), "text-anchor": "middle", text: v.value }));
    }));
  }

  // --------------------------------------------------------- server room --

  function serverRoom(r, wide) {
    const rackH = Math.min(wide ? 300 : 186, r.h - 84);
    const rack = { x: r.x + 20, y: r.y + 64, w: 92, h: rackH };
    const units = [];
    const unitCount = Math.max(3, Math.floor((rackH - 60) / 24));
    const slotIndex = 1;
    let y = rack.y + 8;
    for (let i = 0; i < unitCount + 1; i++) {
      if (i === slotIndex) {
        units.push(s("g", { class: "rack-slot" },
          s("rect", { x: rack.x + 6, y, width: rack.w - 12, height: 40, rx: 3, class: "rack-slot-bg" }),
          s("text", { x: rack.x + 14, y: y + 25, class: "rack-slot-label", text: "AX" }),
          s("circle", { cx: rack.x + rack.w - 22, cy: y + 20, r: 15, class: "lamp-glow", filter: "url(#of-blur)" }),
          s("circle", { cx: rack.x + rack.w - 22, cy: y + 20, r: 8, class: "lamp" })));
        y += 46;
      } else {
        units.push(s("g", { class: "rack-unit-g" },
          s("rect", { x: rack.x + 6, y, width: rack.w - 12, height: 18, rx: 2, class: "rack-unit" }),
          s("circle", { cx: rack.x + 14, cy: y + 9, r: 2, class: "led led-" + (i % 3) }),
          s("circle", { cx: rack.x + 21, cy: y + 9, r: 2, class: "led led-" + ((i + 1) % 3) }),
          s("rect", { x: rack.x + 32, y: y + 7, width: 44, height: 4, rx: 2, class: "rack-vent" })));
        y += 24;
      }
    }
    return s("g", { class: "room room-server" },
      s("rect", { x: r.x, y: r.y, width: r.w, height: r.h, rx: 6, class: "room-floor", fill: "url(#of-tiles)" }),
      s("rect", { x: r.x, y: r.y, width: r.w, height: r.h, rx: 6, class: "wall" }),
      s("text", { x: r.x + 18, y: r.y + 28, class: "room-label", text: "Sala del servidor" }),
      s("text", { x: r.x + 18, y: r.y + 46, class: "room-sub", text: "1 plaza · sandbox AX (gVisor)" }),
      s("rect", { x: rack.x, y: rack.y, width: rack.w, height: rack.h, rx: 6, class: "rack", filter: "url(#of-soft)" }),
      units,
      s("g", { class: "slot-dyn" }),
      s("g", { class: "queue-dyn" }));
  }

  function slotState() {
    const ax = S.snap.ax || {};
    if (!ax.ready) return "bad";
    if (ax.cleanup_failed) return "bad";
    if (S.snap.active || O.sel.activeJob()) return "busy";
    if (ax.blocking) return "busy";
    return "ok";
  }

  function fillSlot(room, r) {
    const state = slotState();
    room.setAttribute("class", "room room-server lamp-" + state);
    const g = room.querySelector(".slot-dyn");
    const x = r.x + 128, y = r.y + 64, w = r.w - 146, hh = 104;
    const ax = S.snap.ax || {};
    const job = O.sel.activeJob();
    const lines = [];
    let href = null, emoji = null, color = null, label;
    if (job) {
      const a = ui.agentOf(job);
      emoji = a.emoji;
      color = a.color;
      href = "#/trabajo/" + O.enc(job.id);
      const title = wrap(job.title || "(sin título)", 22, 2);
      lines.push({ t: title[0], cls: "slot-title" });
      if (title[1]) lines.push({ t: title[1], cls: "slot-title" });
      lines.push({ t: a.name + " · " + (O.own(L.status, job.status) || job.status), cls: "slot-sub" });
      label = "Sandbox ocupado por " + a.name + ": " + (job.title || "");
    } else if (!ax.ready) {
      lines.push({ t: "AX no está listo", cls: "slot-title" });
      lines.push(...wrap(ax.reap_note || "Arrancando o recogiendo ejecuciones anteriores.", 26, 3).map((t) => ({ t, cls: "slot-sub" })));
      label = "AX no está listo";
    } else if (ax.cleanup_failed) {
      lines.push({ t: "Limpieza pendiente", cls: "slot-title" });
      lines.push(...wrap("No se pudo borrar " + ax.cleanup_failed + "; se reintenta.", 26, 3).map((t) => ({ t, cls: "slot-sub" })));
      href = "#/ax";
      label = "Limpieza pendiente de " + ax.cleanup_failed;
    } else if (ax.blocking) {
      emoji = "🖥️";
      lines.push({ t: "Tarea del host", cls: "slot-title" });
      lines.push({ t: u.trunc(ax.blocking, 26), cls: "slot-sub mono" });
      lines.push({ t: "La cola espera a que acabe.", cls: "slot-sub" });
      label = "Ocupado por la tarea del host " + ax.blocking;
    } else {
      emoji = "✨";
      lines.push({ t: "Sandbox libre", cls: "slot-title" });
      lines.push(...wrap("Esperando el próximo encargo de la cola.", 26, 2).map((t) => ({ t, cls: "slot-sub" })));
      label = "Sandbox libre";
    }
    const tx = emoji ? x + 52 : x + 14;
    const card = [
      s("rect", { x, y, width: w, height: hh, rx: 12, class: "slot-card", filter: "url(#of-soft)" }),
      emoji ? s("circle", { cx: x + 28, cy: y + 30, r: 18, class: "slot-avatar", style: color ? { "--agent": u.color(color) } : null }) : null,
      emoji ? s("text", { x: x + 28, y: y + 31, class: "emoji slot-emoji", "text-anchor": "middle", "dominant-baseline": "central", text: emoji }) : null,
      lines.map((l, i) => s("text", { x: tx, y: y + 24 + i * 17, class: l.cls, text: l.t })),
      job ? s("text", { x: x + w - 12, y: y + hh - 12, class: "slot-time", "text-anchor": "end", text: fmt.span(job.started || job.created) }) : null,
    ];
    const content = href
      ? s("a", { href, class: "slot-link", "aria-label": label }, card)
      : s("g", { role: "img", "aria-label": label }, card);
    O.put(g, content);
  }

  function fillQueue(room, r, wide) {
    const g = room.querySelector(".queue-dyn");
    const q = O.sel.queueJobs();
    const x0 = r.x + 128, y0 = r.y + 192, w = r.w - 146;
    const tw = 50, th = 60, gap = 8;
    const perRow = Math.max(1, Math.floor((w + gap) / (tw + gap)));
    const rows = wide ? 3 : 1;
    const cap = perRow * rows;
    const shown = q.length > cap ? q.slice(0, cap - 1) : q;
    const extra = q.length - shown.length;
    const paused = !!S.snap.settings.queue_paused;
    const items = [];
    items.push(s("text", { x: x0, y: y0 - 12, class: "queue-label", text: "Cola · " + q.length + (paused ? " · en pausa" : "") }));
    // rope barrier
    items.push(s("path", { d: `M${x0 - 4} ${y0 - 2} q ${w / 4} 8 ${w / 2} 0 t ${w / 2} 0`, class: "rope" }));
    items.push(s("circle", { cx: x0 - 4, cy: y0 - 2, r: 4, class: "post" }), s("circle", { cx: x0 + w - 4, cy: y0 - 2, r: 4, class: "post" }));
    if (!q.length) {
      items.push(s("text", { x: x0 + w / 2, y: y0 + 34, class: "queue-empty", "text-anchor": "middle", text: "Nadie en la cola" }));
    }
    shown.forEach((j, i) => {
      const a = ui.agentOf(j);
      const cx = x0 + (i % perRow) * (tw + gap), cy = y0 + 8 + Math.floor(i / perRow) * (th + gap);
      items.push(s("a", { href: "#/trabajo/" + O.enc(j.id), class: "ticket-link", "aria-label": "En cola #" + (i + 1) + ": " + a.name + ", " + (j.title || "") },
        s("title", { text: "#" + (i + 1) + " · " + a.name + " · " + (j.title || "") + (j.waiting ? " — " + j.waiting : "") }),
        s("rect", { x: cx, y: cy, width: tw, height: th, rx: 9, class: "qticket" + (j.priority === 2 ? " is-hot" : ""), style: { "--agent": u.color(a.color) } }),
        s("rect", { x: cx, y: cy, width: tw, height: 6, rx: 3, class: "qticket-band", style: { "--agent": u.color(a.color) } }),
        s("text", { x: cx + tw / 2, y: cy + 28, class: "emoji qticket-emoji", "text-anchor": "middle", "dominant-baseline": "central", text: a.emoji }),
        s("text", { x: cx + tw / 2, y: cy + 51, class: "qticket-pos", "text-anchor": "middle", text: "#" + (i + 1) })));
    });
    if (extra > 0) {
      const i = shown.length;
      const cx = x0 + (i % perRow) * (tw + gap), cy = y0 + 8 + Math.floor(i / perRow) * (th + gap);
      items.push(s("a", { href: "#/tablero", class: "ticket-link", "aria-label": extra + " más en la cola" },
        s("rect", { x: cx, y: cy, width: tw, height: th, rx: 9, class: "qticket is-more" }),
        s("text", { x: cx + tw / 2, y: cy + 34, class: "qticket-more", "text-anchor": "middle", text: "+" + extra })));
    }
    if (paused) {
      items.push(s("g", { class: "pause-sign", transform: `translate(${x0 + w / 2} ${y0 - 2})` },
        s("path", { d: "M-14 0l14 -10 14 10", class: "sign-string" }),
        s("rect", { x: -44, y: 0, width: 88, height: 22, rx: 5, class: "sign" }),
        s("text", { x: 0, y: 15, class: "sign-text", "text-anchor": "middle", text: "⏸ EN PAUSA" })));
    }
    O.put(g, items);
  }

  // -------------------------------------------------------- meeting room --

  function meetingRoom(r) {
    return s("g", { class: "room room-meet" },
      s("rect", { x: r.x, y: r.y, width: r.w, height: r.h, rx: 6, class: "room-floor", fill: "url(#of-carpet)" }),
      s("rect", { x: r.x, y: r.y, width: r.w, height: r.h, rx: 6, class: "wall" }),
      s("text", { x: r.x + 18, y: r.y + 28, class: "room-label", text: "Sala de reuniones" }),
      plant(r.x + r.w - 26, r.y + r.h - 34, 0.8),
      s("g", { class: "meet-dyn" }));
  }

  function currentPipeline() {
    const running = O.sel.runningPipelines();
    if (!running.length) return { p: null, others: 0 };
    const act = O.sel.activeJob();
    const p = (act && running.find((x) => x.id === act.pipeline_id)) || running[0];
    return { p, others: running.length - 1 };
  }

  function fillMeeting(room, r) {
    const g = room.querySelector(".meet-dyn");
    const { p, others } = currentPipeline();
    const cx = r.x + r.w / 2;
    const screen = { x: cx - 128, y: r.y + 44, w: 256, h: 48 };
    const tableCy = screen.y + screen.h + (r.h - (screen.y + screen.h - r.y)) / 2 - 2;
    const rx = Math.min(104, r.w / 2 - 82), ry = Math.min(44, (r.h - 170) / 2);
    const items = [];
    const tpl = p ? O.sel.template(p.template) : null;
    let line1, line2;
    if (p) {
      const idx = p.steps.findIndex((st) => O.isRunning(st.status) || st.status === "en_cola");
      const step = idx >= 0 ? p.steps[idx] : null;
      line1 = (tpl ? tpl.icon + " " : "") + u.trunc(p.title || (tpl && tpl.name) || p.template, 34);
      line2 = (step ? "Paso " + (idx + 1) + "/" + p.steps.length + ": " + step.name : "Entre pasos") +
        (p.max_iterations > 1 && tpl && tpl.iterative ? " · ronda " + Math.max(1, p.iteration || 1) + "/" + p.max_iterations : "");
    } else {
      line1 = "Sala libre";
      line2 = "Crea un equipo para reunir a varios agentes";
    }
    items.push(
      s("rect", { x: screen.x, y: screen.y, width: screen.w, height: screen.h, rx: 6, class: "meet-screen" + (p ? " is-on" : "") }),
      s("text", { x: cx, y: screen.y + 20, class: "meet-screen-title", "text-anchor": "middle", text: line1 }),
      s("text", { x: cx, y: screen.y + 37, class: "meet-screen-sub", "text-anchor": "middle", text: u.trunc(line2, 44) }));
    // seats around the table
    const seats = [];
    if (p) {
      const roles = tpl ? tpl.roles : Object.keys(p.participants).map((role) => ({ role, label: role }));
      const activeStep = p.steps.find((st) => O.isRunning(st.status));
      for (const role of roles) {
        const agentId = p.participants[role.role] || role.default_agent;
        const agent = O.sel.agent(agentId);
        seats.push({
          agent, label: role.label || role.role,
          speaking: !!activeStep && activeStep.role === role.role,
        });
      }
    }
    const n = Math.max(4, Math.min(6, seats.length || 4));
    items.push(s("ellipse", { cx, cy: tableCy + 4, rx: rx + 6, ry: ry + 6, class: "meet-table-shadow" }));
    items.push(s("ellipse", { cx, cy: tableCy, rx, ry, class: "meet-table" }));
    for (let i = 0; i < n; i++) {
      const ang = -Math.PI / 2 + (2 * Math.PI * i) / n + Math.PI / n;
      const sx = cx + (rx + 30) * Math.cos(ang), sy = tableCy + (ry + 26) * Math.sin(ang);
      const seat = seats[i];
      const parts = [s("circle", { cx: sx, cy: sy, r: 19, class: "meet-chair" })];
      // a laptop on the table in front of each seat
      const lx = cx + (rx - 22) * Math.cos(ang), ly = tableCy + (ry - 14) * Math.sin(ang);
      parts.push(s("rect", { x: lx - 9, y: ly - 6, width: 18, height: 12, rx: 2, class: "meet-laptop" + (seat && seat.speaking ? " is-on" : "") }));
      if (seat && seat.agent) {
        parts.push(s("g", { class: "meet-person" + (seat.speaking ? " is-speaking" : ""), style: { "--agent": u.color(seat.agent.color) } },
          s("circle", { cx: sx, cy: sy, r: 16, class: "meet-avatar" }),
          s("text", { x: sx, y: sy + 1, class: "emoji meet-emoji", "text-anchor": "middle", "dominant-baseline": "central", text: seat.agent.emoji }),
          seat.speaking ? s("text", { x: sx + 16, y: sy - 14, class: "emoji meet-talk", text: "💬" }) : null));
        parts.push(s("text", { x: sx, y: sy + (Math.sin(ang) > 0 ? 32 : -24), class: "meet-role", "text-anchor": "middle", text: u.trunc(seat.agent.name + " · " + seat.label, 22) }));
      } else if (seat) {
        parts.push(s("text", { x: sx, y: sy + 32, class: "meet-role", "text-anchor": "middle", text: seat.label }));
      }
      items.push(s("g", null, parts));
    }
    if (others > 0) {
      items.push(s("text", { x: r.x + 18, y: r.y + r.h - 16, class: "room-sub", text: "+" + others + " " + u.plural(others, "equipo esperando", "equipos esperando") }));
    }
    const label = p ? "Sala de reuniones: " + line1 + ". " + line2 : "Sala de reuniones libre. Crear un equipo";
    O.put(g, s("a", { href: p ? "#/equipos/" + O.enc(p.id) : "#/nuevo?tab=equipo", class: "meet-link", "aria-label": label }, items));
  }

  // ------------------------------------------------------------- the floor --

  function openFloor(lay) {
    const f = lay.floor;
    const parts = [
      s("rect", { x: f.x, y: f.y, width: f.w, height: f.h, rx: 6, class: "room-floor", fill: "url(#of-wood)" }),
    ];
    // rug under the desks
    const rugY = lay.deskTop - 16, rugH = lay.rows * lay.cellH + 12;
    parts.push(s("rect", { x: f.x + 14, y: rugY, width: f.w - 28, height: rugH, rx: 26, class: "rug" }));
    parts.push(s("rect", { x: f.x, y: f.y, width: f.w, height: f.h, rx: 6, class: "wall" }));
    if (lay.wide) {
      // windows in the top wall, between the whiteboard and the coffee bar
      parts.push(s("g", { class: "windows" },
        s("rect", { x: 500, y: 19, width: 96, height: 10, rx: 2, class: "window" }),
        s("rect", { x: 500, y: 19, width: 96, height: 10, rx: 2, class: "window-frame" })));
      // coffee bar
      parts.push(s("g", { class: "coffee-bar" },
        s("rect", { x: 628, y: 44, width: 228, height: 52, rx: 8, class: "counter", filter: "url(#of-soft)" }),
        s("rect", { x: 646, y: 50, width: 34, height: 38, rx: 5, class: "coffee-machine" }),
        s("circle", { cx: 663, cy: 62, r: 6, class: "coffee-dial" }),
        s("rect", { x: 656, y: 76, width: 14, height: 9, rx: 2, class: "mug" }),
        s("path", { d: "M660 72c-3 -4 3 -6 0 -10M666 72c-3 -4 3 -6 0 -10", class: "steam always" }),
        s("rect", { x: 700, y: 58, width: 12, height: 14, rx: 3, class: "mug alt" }),
        s("rect", { x: 718, y: 58, width: 12, height: 14, rx: 3, class: "mug" }),
        s("circle", { cx: 770, cy: 66, r: 13, class: "fruit-bowl" }),
        s("circle", { cx: 765, cy: 62, r: 4.5, class: "fruit f1" }),
        s("circle", { cx: 774, cy: 63, r: 4.5, class: "fruit f2" }),
        s("circle", { cx: 770, cy: 70, r: 4.5, class: "fruit f3" }),
        s("rect", { x: 808, y: 48, width: 30, height: 42, rx: 6, class: "cooler" }),
        s("rect", { x: 812, y: 52, width: 22, height: 16, rx: 6, class: "cooler-bottle" }),
        s("text", { x: 742, y: 112, class: "room-sub", "text-anchor": "middle", text: "☕ Rincón del café" })));
      // wall clock
      parts.push(s("g", { class: "clock", transform: "translate(548 92)" },
        s("circle", { r: 24, class: "clock-face", filter: "url(#of-soft)" }),
        s("circle", { r: 20, class: "clock-dial" }),
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11].map((k) => s("line", {
          x1: 0, y1: -17, x2: 0, y2: k % 3 === 0 ? -13 : -15, class: "clock-tick", transform: `rotate(${k * 30})`,
        })),
        s("line", { x1: 0, y1: 2, x2: 0, y2: -10, class: "clock-hand hour" }),
        s("line", { x1: 0, y1: 3, x2: 0, y2: -15, class: "clock-hand minute" }),
        s("circle", { r: 2, class: "clock-pin" })));
      parts.push(plant(f.x + 30, f.y + f.h - 40, 1.1));
      parts.push(plant(f.x + f.w - 32, f.y + f.h - 40, 1.1));
      parts.push(plant(612, 70, 0.75));
      parts.push(s("rect", { x: f.x + f.w / 2 - 40, y: f.y + f.h - 5, width: 80, height: 10, rx: 2, class: "door" }));
      parts.push(s("text", { x: f.x + 76, y: f.y + f.h - 14, class: "room-sub", text: "Planta abierta" }));
    } else {
      parts.push(s("text", { x: f.x + 18, y: f.y + 28, class: "room-label", text: "Planta abierta" }));
      parts.push(s("text", { x: f.x + 18, y: f.y + 46, class: "room-sub", text: "Toca un escritorio para ver al agente" }));
      parts.push(s("g", { class: "clock", transform: `translate(${f.x + f.w - 30} ${f.y + 30})` },
        s("circle", { r: 17, class: "clock-face" }),
        s("circle", { r: 14, class: "clock-dial" }),
        s("line", { x1: 0, y1: 2, x2: 0, y2: -7, class: "clock-hand hour" }),
        s("line", { x1: 0, y1: 2, x2: 0, y2: -11, class: "clock-hand minute" }),
        s("circle", { r: 1.6, class: "clock-pin" })));
      parts.push(plant(f.x + f.w - 70, f.y + 30, 0.6));
    }
    return s("g", { class: "room room-floor-g" }, parts);
  }

  function setClock(svg) {
    const d = new Date(O.now());
    const hr = d.getHours() % 12, mi = d.getMinutes();
    for (const c of svg.querySelectorAll(".clock")) {
      const hh = c.querySelector(".hour"), mm = c.querySelector(".minute");
      if (hh) hh.setAttribute("transform", `rotate(${hr * 30 + mi * 0.5})`);
      if (mm) mm.setAttribute("transform", `rotate(${mi * 6})`);
    }
    for (const w of svg.querySelectorAll(".window")) w.setAttribute("class", "window " + skyClass());
  }

  // ------------------------------------------------------------- the scene --

  function enabledAgents() {
    return O.sel.agents().filter((a) => a.enabled);
  }

  function structureSig(wide) {
    return (wide ? "w" : "n") + "|" + enabledAgents().map((a) => [a.id, a.name, a.emoji, a.color, a.role, a.model, a.effort, a.mode, a.harness].join(",")).join("|");
  }

  function buildScene(stage) {
    const wide = stage.clientWidth >= WIDE_MIN;
    const agents = enabledAgents();
    const lay = layout(agents.length, wide);
    const desks = new Map();
    const deskLayer = s("g", { class: "desks" });
    const bubbleLayer = s("g", { class: "bubbles" });
    stage.classList.toggle("is-narrow", !wide);
    agents.forEach((a, i) => {
      const refs = buildDesk(a, i, lay.desks[i], !wide);
      desks.set(a.id, refs);
      deskLayer.appendChild(refs.g);
      bubbleLayer.appendChild(refs.bubble);
      const open = () => openAgentDrawer(a.id);
      refs.g.addEventListener("click", open);
      refs.g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      });
    });
    const server = serverRoom(lay.server, wide);
    const meeting = meetingRoom(lay.meeting);
    const board = whiteboard(lay.board);
    const boardBg = lay.wide ? null : s("rect", { x: 8, y: 8, width: lay.W - 16, height: lay.board.h + 16, rx: 8, class: "wall-panel" });
    const svg = s("svg", {
      class: "office-svg" + (wide ? " is-wide" : " is-narrow"),
      viewBox: `0 0 ${lay.W} ${lay.H}`, role: "group",
      "aria-label": "Plano de la oficina: escritorios de los agentes, sala del servidor y sala de reuniones",
    },
    defs(),
    s("rect", { x: 0, y: 0, width: lay.W, height: lay.H, class: "office-bg" }),
    boardBg,
    openFloor(lay), server, meeting, board, deskLayer, bubbleLayer,
    agents.length ? null : s("text", { x: lay.floor.x + lay.floor.w / 2, y: lay.deskTop + 60, class: "room-sub", "text-anchor": "middle", text: "No hay agentes activos: actívalos en Agentes." }));
    O.put(stage, svg);
    return { svg, lay, desks, server, meeting, board, wide };
  }

  function updateScene(sc) {
    for (const a of enabledAgents()) {
      const refs = sc.desks.get(a.id);
      if (refs) updateDesk(refs, a);
    }
    const slotSig = JSON.stringify([slotState(), S.snap.active && S.snap.active.job_id, (O.sel.activeJob() || {}).status, (O.sel.activeJob() || {}).title, S.snap.ax]);
    if (sc.slotSig !== slotSig) {
      sc.slotSig = slotSig;
      fillSlot(sc.server, sc.lay.server);
    }
    const qSig = O.sel.queueJobs().map((j) => j.id + j.priority + (j.waiting || "")).join(",") + S.snap.settings.queue_paused;
    if (sc.qSig !== qSig) {
      sc.qSig = qSig;
      fillQueue(sc.server, sc.lay.server, sc.wide);
    }
    const { p } = currentPipeline();
    const mSig = p ? JSON.stringify([p.id, p.steps.map((x) => x.status), p.iteration, O.sel.runningPipelines().length]) : "none";
    if (sc.mSig !== mSig) {
      sc.mSig = mSig;
      fillMeeting(sc.meeting, sc.lay.meeting);
    }
    fillWhiteboard(sc.board.querySelector(".wb-values"), sc.lay.board);
    setClock(sc.svg);
  }

  // -------------------------------------------------------- agent drawer --

  function metricFor(agent) {
    return (S.snap.metrics.by_agent_version || []).find((m) => m.agent_id === agent.id && m.version === agent.version) || null;
  }

  function statTiles(m) {
    const t = (label, value) => h("div", { class: "stat" }, h("span", { class: "stat-value", text: value }), h("span", { class: "stat-label", text: label }));
    if (!m || !m.jobs) return h("p", { class: "muted", text: "Esta versión aún no ha terminado ningún trabajo." });
    return h("div", { class: "stats" },
      t("trabajos", fmt.num(m.jobs)),
      t("éxito", fmt.pct(m.jobs ? m.succeeded / m.jobs : NaN)),
      t("👍 / 👎", m.thumbs_up + " / " + m.thumbs_down),
      t("coste medio", fmt.usd(m.avg_cost_usd)),
      t("duración media", fmt.duration(m.avg_seconds * 1000)));
  }

  function openAgentDrawer(agentId) {
    let timeline = null;
    let timelineJob = "";
    const info = h("div");
    const live = h("div");
    const rest = h("div");

    const render = () => {
      const agent = O.sel.agent(agentId);
      if (!agent) {
        ui.drawer.close();
        return;
      }
      const st = O.sel.agentState(agentId);
      const sig = JSON.stringify([agent, st.state, st.job && [st.job.id, st.job.status, st.job.activity, st.job.stalled],
        st.queued.map((j) => j.id), st.attention.map((j) => j.id), st.last && st.last.id]);
      if (info._sig !== sig) {
        info._sig = sig;
        O.put(info,
          h("div", { class: "agent-hero" },
            ui.avatar(agent, "xl"),
            h("div", null,
              h("p", { class: "agent-hero-role", text: agent.role }),
              h("p", { class: "muted mono", text: (O.own(L.harness, agent.harness) || agent.harness) + " · " + ui.modelLabel(agent.harness, agent.model, agent.effort) }),
              h("p", { class: "chips" },
                h("span", { class: "badge badge-soft", title: (O.own(L.modes, agent.mode) || {}).hint || null, text: "Modo " + ((O.own(L.modes, agent.mode) || {}).label || agent.mode).toLowerCase() }),
                h("span", { class: "badge badge-soft", text: "v" + agent.version }),
                h("span", { class: "badge badge-soft", text: O.own(STATE_TEXT, st.state) })))),
          h("div", { class: "row-actions" },
            ui.btn("Encargar trabajo", { href: "#/nuevo?agente=" + O.enc(agent.id), kind: "primary", icon: "plus", size: "sm" }),
            ui.btn("Editar", { href: "#/agentes/" + O.enc(agent.id), icon: "edit", size: "sm" })),
          st.job ? h("div", { class: "drawer-now" },
            h("h3", { class: "drawer-h", text: "Ahora mismo" }),
            ui.jobCard(st.job),
            h("div", { class: "row-actions" },
              ui.btn("Abrir trabajo", { href: "#/trabajo/" + O.enc(st.job.id), size: "sm", icon: "eye" }),
              ui.btn("Cancelar", {
                size: "sm", kind: "danger-ghost", icon: "stop",
                onClick: async (e) => {
                  const btn = e.currentTarget;
                  if (!(await O.confirm({ title: "¿Cancelar el trabajo?", text: "El agente se detiene y la tarea del sandbox se borra. Lo hecho hasta ahora se pierde (salvo el registro).", confirmLabel: "Cancelar trabajo", danger: true }))) return;
                  await ui.busy(btn, () => O.api.post("/api/jobs/" + O.enc(st.job.id) + "/cancel", {}), "No se pudo cancelar");
                },
              }))) : null);
      }
      // live log of the current job
      const jobId = st.job ? st.job.id : "";
      if (jobId !== timelineJob) {
        if (timeline) timeline.destroy();
        timeline = null;
        timelineJob = jobId;
        O.stream.watch("drawer", jobId || null);
        if (jobId) {
          timeline = O.timeline({ jobId, compact: true });
          O.put(live, h("h3", { class: "drawer-h", text: "En directo" }), timeline.el);
        } else {
          live.replaceChildren();
        }
      }
      const rsig = sig + JSON.stringify(metricFor(agent));
      if (rest._sig !== rsig) {
        rest._sig = rsig;
        const recent = O.sel.jobs().filter((j) => j.agent_id === agentId && O.isFinished(j.status)).slice(0, 5);
        O.put(rest,
          st.attention.length ? h("div", null, h("h3", { class: "drawer-h", text: "Te espera" }),
            h("div", { class: "card-list" }, st.attention.map((j) => ui.jobCard(j)))) : null,
          st.queued.length ? h("div", null, h("h3", { class: "drawer-h", text: "En cola" }),
            h("div", { class: "card-list" }, st.queued.map((j) => ui.jobCard(j)))) : null,
          h("h3", { class: "drawer-h", text: "Versión actual (v" + agent.version + ")" }),
          statTiles(metricFor(agent)),
          h("h3", { class: "drawer-h", text: "Últimos trabajos" }),
          recent.length ? h("div", { class: "card-list" }, recent.map((j) => ui.jobCard(j)))
            : h("p", { class: "muted", text: "Todavía no ha terminado ningún trabajo." }));
      }
    };

    const agent = O.sel.agent(agentId);
    const off = O.on("store", render);
    ui.drawer.open({
      title: h("span", null, agent ? agent.name : agentId),
      body: [info, live, rest],
      onClose: () => {
        off();
        if (timeline) timeline.destroy();
        O.stream.watch("drawer", null);
      },
    });
    render();
  }
  O.openAgentDrawer = openAgentDrawer;

  // ------------------------------------------------------------------ view --

  function headline() {
    const act = O.sel.activeJob();
    const q = O.sel.queueJobs().length;
    const inbox = O.sel.inboxCount();
    const parts = [];
    if (act) parts.push(ui.agentOf(act).name + " está trabajando en «" + u.trunc(act.title, 48) + "»");
    else parts.push("Nadie trabaja ahora mismo");
    if (q) parts.push(q + " en cola");
    if (inbox) parts.push(inbox + " " + u.plural(inbox, "cosa te espera", "cosas te esperan") + " en la Bandeja");
    return parts.join(" · ");
  }

  function legend() {
    const item = (cls, label) => h("li", { class: "legend-item " + cls }, h("span", { class: "legend-dot", "aria-hidden": "true" }), label);
    return h("ul", { class: "legend", "aria-label": "Leyenda de los monitores" },
      item("is-working", "Trabajando"), item("is-attention", "Te necesita"), item("is-queued", "Tiene trabajo en cola"),
      item("is-idle", "Libre"), item("is-sleeping", "Descansando"));
  }

  function recentRow(j) {
    const a = ui.agentOf(j);
    const icon = j.status === "hecho" ? "✅" : j.status === "fallido" ? "⛔" : "⏹️";
    return h("li", null, h("a", { class: "recent-row", href: "#/trabajo/" + O.enc(j.id) },
      h("span", { class: "recent-icon", "aria-hidden": "true", text: icon }),
      ui.avatar(a, "xs"),
      h("span", { class: "recent-title", text: j.title || "(sin título)" }),
      h("span", { class: "recent-meta muted" }, ui.time(j.finished || j.created)),
      j.usage && j.usage.cost_usd > 0 ? h("span", { class: "recent-cost muted", text: fmt.usd(j.usage.cost_usd) }) : null));
  }

  O.route("/", {
    title: "Oficina",
    mount(root) {
      const sub = h("span");
      const stage = h("div", { class: "office-stage" });
      const now = h("div");
      const queue = h("div");
      const recent = h("div");
      O.put(root,
        ui.pageHead({
          title: S.snap.settings.office_name || "Oficina",
          subtitle: sub,
          actions: [
            ui.btn("Nuevo trabajo", { href: "#/nuevo", kind: "primary", icon: "plus" }),
            ui.btn("Nuevo equipo", { href: "#/nuevo?tab=equipo", icon: "team" }),
          ],
        }),
        h("div", { class: "office-wrap" }, stage, legend()),
        h("div", { class: "office-panels" },
          ui.section("Ahora mismo", { icon: "⚡" }, now),
          ui.section("Siguientes en la cola", { icon: "🎫", actions: ui.btn("Tablero", { href: "#/tablero", size: "sm", kind: "ghost" }) }, queue),
          ui.section("Actividad reciente", { icon: "🕘" }, recent)));

      let scene = null;
      let sig = "";
      const draw = () => {
        const nsig = structureSig(stage.clientWidth >= WIDE_MIN);
        if (!scene || nsig !== sig) {
          sig = nsig;
          scene = buildScene(stage);
        }
        updateScene(scene);
      };
      const panels = () => {
        sub.textContent = headline();
        const act = O.sel.activeJob();
        ui.memo(now, JSON.stringify(act ? [act.id, act.status, act.activity, act.stalled, act.usage] : S.snap.ax), () => act
          ? ui.jobCard(act)
          : ui.empty({
            cls: "empty-sm", icon: "🌙",
            title: S.snap.ax.ready ? "El sandbox está libre" : "AX aún no está listo",
            text: S.snap.ax.ready
              ? "Solo hay un sandbox: los agentes trabajan de uno en uno y el resto espera en la cola."
              : S.snap.ax.reap_note || "El ejecutor está arrancando.",
          }));
        const q = O.sel.queueJobs();
        ui.memo(queue, q.map((j) => j.id + j.status + (j.waiting || "") + j.priority).join(","), () => q.length
          ? h("div", { class: "card-list" }, q.slice(0, 5).map((j, i) => ui.jobCard(j, { position: i + 1, status: false })),
            q.length > 5 ? h("a", { class: "more-link-inline", href: "#/tablero", text: "y " + (q.length - 5) + " más…" }) : null)
          : ui.empty({ cls: "empty-sm", icon: "🎫", text: "La cola está vacía. Lo que encargues se pondrá aquí por orden de prioridad." }));
        const done = O.sel.jobs().filter((j) => O.isFinished(j.status)).slice(0, 8);
        ui.memo(recent, done.map((j) => j.id + j.status).join(","), () => done.length
          ? h("ul", { class: "recent-list" }, done.map(recentRow))
          : ui.empty({ cls: "empty-sm", icon: "🗒️", text: "Aquí verás los últimos trabajos terminados." }));
      };
      draw();
      panels();

      let ro = null;
      if (typeof ResizeObserver !== "undefined") {
        let last = stage.clientWidth >= WIDE_MIN;
        ro = new ResizeObserver(() => {
          const w = stage.clientWidth >= WIDE_MIN;
          if (w !== last) {
            last = w;
            draw();
          }
        });
        ro.observe(stage);
      }
      const offTick = O.on("tick", () => { if (scene) { setClock(scene.svg); updateScene(scene); } });
      return {
        update() {
          draw();
          panels();
        },
        unmount() {
          if (ro) ro.disconnect();
          offTick();
        },
      };
    },
  });
})();
