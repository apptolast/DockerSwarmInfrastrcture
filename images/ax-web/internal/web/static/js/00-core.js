/*
 * Oficina de agentes · core.
 *
 * Every script of the UI is an IIFE that extends window.Oficina; the server
 * concatenates them in lexical order. This file defines the DOM builder
 * (text always becomes text nodes, never markup), es-ES formatting, the
 * API client, the event bus, icons, toasts and dialogs. Loading it has no
 * side effects beyond defining those helpers.
 */
(() => {
  "use strict";
  const O = (window.Oficina = window.Oficina || {});
  const SVGNS = "http://www.w3.org/2000/svg";
  O.SVGNS = SVGNS;

  // ---------------------------------------------------------------- DOM --

  // Properties set on the element object instead of as attributes.
  const PROPS = new Set(["value", "checked", "selected", "disabled", "multiple",
    "indeterminate", "readOnly", "required"]);
  // Attributes that hold a URL: only same-origin paths, fragments and
  // http(s) URLs are accepted.
  const URL_ATTRS = new Set(["href", "src", "action", "formaction", "xlink:href", "poster"]);
  // Attributes that load or run content, or change what an element is:
  // never set through h(), whatever the value.
  const BANNED_ATTRS = new Set(["srcdoc", "data", "ping", "http-equiv", "is"]);

  function safeLink(value) {
    const raw = String(value);
    // Browsers drop tabs and newlines inside URLs and read "\" as "/", so
    // "/\t/evil.com" or "/\\evil.com" would leave the site: refuse them all.
    if (/[\u0000-\u001f\u007f\\]/.test(raw)) return null;
    const s = raw.trim();
    if (s === "") return null;
    if (s[0] === "#") return s;
    if (s[0] === "/" && s[1] !== "/" && s[1] !== "\\") return s;
    if (/^https?:\/\/[^\s\u0000-\u001f]+$/i.test(s)) return s;
    return null;
  }
  O.safeLink = safeLink;

  function add(el, child) {
    if (child === null || child === undefined || child === false || child === true) return;
    if (Array.isArray(child)) {
      for (const c of child) add(el, c);
      return;
    }
    if (typeof child === "object" && child.nodeType) {
      el.appendChild(child);
      return;
    }
    el.appendChild(document.createTextNode(String(child)));
  }

  function isProps(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v) && !v.nodeType;
  }

  function build(el, props, children) {
    const svg = el.namespaceURI === SVGNS;
    if (props) {
      for (const key of Object.keys(props)) {
        const v = props[key];
        if (v === undefined || v === null || v === false) continue;
        if (key === "class") {
          const c = Array.isArray(v) ? v.filter(Boolean).join(" ") : String(v);
          if (c) el.setAttribute("class", c);
        } else if (key === "text") {
          el.textContent = String(v);
        } else if (key === "on") {
          for (const ev of Object.keys(v)) if (v[ev]) el.addEventListener(ev, v[ev]);
        } else if (key === "style") {
          if (typeof v !== "object") throw new Error("h: style must be an object (CSSOM)");
          for (const p of Object.keys(v)) {
            if (v[p] !== null && v[p] !== undefined) el.style.setProperty(p, String(v[p]));
          }
        } else if (key === "ref") {
          v(el);
        } else if (key === "dataset") {
          for (const d of Object.keys(v)) el.dataset[d] = String(v[d]);
        } else if (/^on/i.test(key)) {
          throw new Error("h: inline event attributes are not allowed: " + key);
        } else if (BANNED_ATTRS.has(key.toLowerCase())) {
          throw new Error("h: attribute not allowed: " + key);
        } else if (!svg && PROPS.has(key)) {
          el[key] = v;
        } else if (URL_ATTRS.has(key)) {
          const safe = safeLink(v);
          if (safe !== null) el.setAttribute(key, safe);
        } else {
          el.setAttribute(key, v === true ? "" : String(v));
        }
      }
    }
    add(el, children);
    return el;
  }

  /** h(tag, props?, ...children) builds an HTML element. */
  O.h = function h(tag, props, ...children) {
    if (!isProps(props)) {
      if (props !== undefined) children.unshift(props);
      props = null;
    }
    return build(document.createElement(tag), props, children);
  };

  /** s(tag, props?, ...children) builds an SVG element. */
  O.s = function s(tag, props, ...children) {
    if (!isProps(props)) {
      if (props !== undefined) children.unshift(props);
      props = null;
    }
    return build(document.createElementNS(SVGNS, tag), props, children);
  };

  O.frag = (...children) => {
    const f = document.createDocumentFragment();
    add(f, children);
    return f;
  };

  /** Replaces the children of el. */
  O.put = (el, ...children) => {
    el.replaceChildren();
    add(el, children);
    return el;
  };

  // ---------------------------------------------------------------- bus --

  const listeners = new Map();
  O.on = (evt, fn) => {
    if (!listeners.has(evt)) listeners.set(evt, new Set());
    listeners.get(evt).add(fn);
    return () => listeners.get(evt).delete(fn);
  };
  O.emit = (evt, data) => {
    const set = listeners.get(evt);
    if (!set) return;
    for (const fn of Array.from(set)) {
      try {
        fn(data);
      } catch (e) {
        if (typeof console !== "undefined") console.error("Oficina:", evt, e);
      }
    }
  };

  // --------------------------------------------------------------- util --

  const enc = typeof TextEncoder !== "undefined" ? new TextEncoder() : null;
  let uidCounter = 0;
  O.u = {
    uid: (p) => (p || "id") + "-" + (++uidCounter).toString(36),
    bytes: (s) => (enc ? enc.encode(String(s || "")).length : String(s || "").length),
    runes: (s) => Array.from(String(s || "")).length,
    trunc(s, n) {
      const a = Array.from(String(s || ""));
      return a.length > n ? a.slice(0, Math.max(0, n - 1)).join("") + "…" : a.join("");
    },
    oneLine: (s) => String(s || "").replace(/\s+/g, " ").trim(),
    color: (c) => (/^#[0-9a-fA-F]{6}$/.test(String(c || "")) ? c : "#64748b"),
    clamp: (n, a, b) => Math.min(b, Math.max(a, n)),
    plural: (n, one, many) => (n === 1 ? one : many),
    debounce(fn, ms) {
      let t = null;
      return (...args) => {
        clearTimeout(t);
        t = setTimeout(() => fn(...args), ms);
      };
    },
    sortBy(arr, key, desc) {
      const f = typeof key === "function" ? key : (x) => x[key];
      return arr.slice().sort((a, b) => {
        const x = f(a), y = f(b);
        const r = x < y ? -1 : x > y ? 1 : 0;
        return desc ? -r : r;
      });
    },
    /** ms since epoch, or NaN for empty and Go zero times. */
    ms(t) {
      if (!t) return NaN;
      if (typeof t === "number") return t;
      const v = Date.parse(t);
      return v > 0 ? v : NaN;
    },
  };

  /** obj[key] only when it is obj's own property (never Object.prototype's). */
  O.own = (obj, key) => (obj !== null && obj !== undefined && Object.hasOwn(obj, key) ? obj[key] : undefined);

  // ------------------------------------------------- invisible characters --

  // Format (Cf: bidi overrides and isolates, zero-width, tags…) and control
  // (Cc) characters plus U+2028/U+2029, except "\n" and "\t".
  const RE_INVISIBLE = /[\p{Cf}\u0000-\u0008\u000b-\u001f\u007f-\u009f\u2028\u2029]/u;
  const RE_INVISIBLE_G = /[\p{Cf}\u0000-\u0008\u000b-\u001f\u007f-\u009f\u2028\u2029]/gu;
  // A zero-width joiner inside an emoji sequence (woman + ZWJ + laptop) only draws the emoji.
  const RE_PICT_BEFORE = /\p{Extended_Pictographic}[\uFE0F\u{1F3FB}-\u{1F3FF}]?$/u;
  const RE_PICT_AFTER = /^\p{Extended_Pictographic}/u;
  const joinsEmoji = (s, off) =>
    RE_PICT_BEFORE.test(s.slice(Math.max(0, off - 4), off)) && RE_PICT_AFTER.test(s.slice(off + 1, off + 3));

  const str0 = (v) => (v === null || v === undefined ? "" : String(v));
  const mark = (ch) => "⟦U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + "⟧";

  /**
   * visible(str) replaces every invisible or control character (bidi
   * controls, zero-width characters, tags, C0/C1 controls, U+2028/U+2029;
   * not "\n" nor "\t") with a visible marker like ⟦U+202E⟧, so that text a
   * human reviews before trusting it cannot hide or reorder anything.
   */
  O.visible = function visible(str) {
    const s = str0(str);
    if (!RE_INVISIBLE.test(s)) return s;
    return s.replace(RE_INVISIBLE_G, (ch, off) => (ch === "\u200d" && joinsEmoji(s, off) ? ch : mark(ch)));
  };

  /** The characters visible(str) marks, as "⟦U+XXXX⟧" strings in order. */
  O.invisibles = function invisibles(str) {
    const s = str0(str);
    const out = [];
    if (!RE_INVISIBLE.test(s)) return out;
    for (const m of s.matchAll(RE_INVISIBLE_G)) {
      if (!(m[0] === "\u200d" && joinsEmoji(s, m.index))) out.push(mark(m[0]));
    }
    return out;
  };
  O.hasInvisible = (str) => O.invisibles(str).length > 0;

  /** Spanish summary of the invisible characters of str, or "". */
  O.invisibleWarning = function invisibleWarning(str) {
    const all = O.invisibles(str);
    if (!all.length) return "";
    const kinds = Array.from(new Set(all));
    return (all.length === 1 ? "Hay 1 carácter invisible o de control" : "Hay " + all.length + " caracteres invisibles o de control") +
      " (" + kinds.slice(0, 6).join(" ") + (kinds.length > 6 ? " …" : "") + "): pueden ocultar o reordenar el texto.";
  };

  // ---------------------------------------------------------- formatting --

  const LOC = "es-ES";
  const NF = new Intl.NumberFormat(LOC);
  const NF1 = new Intl.NumberFormat(LOC, { maximumFractionDigits: 1 });
  const NF0 = new Intl.NumberFormat(LOC, { maximumFractionDigits: 0 });
  const USD4 = new Intl.NumberFormat(LOC, { style: "currency", currency: "USD", minimumFractionDigits: 4, maximumFractionDigits: 4 });
  const USD2 = new Intl.NumberFormat(LOC, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const DT = new Intl.DateTimeFormat(LOC, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const DTY = new Intl.DateTimeFormat(LOC, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const DFULL = new Intl.DateTimeFormat(LOC, { weekday: "long", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const DAY = new Intl.DateTimeFormat(LOC, { weekday: "long", day: "numeric", month: "long" });
  const TM = new Intl.DateTimeFormat(LOC, { hour: "2-digit", minute: "2-digit" });
  const TMS = new Intl.DateTimeFormat(LOC, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const UTC_TM = new Intl.DateTimeFormat(LOC, { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  const UTC_DT = new Intl.DateTimeFormat(LOC, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  const RTF = new Intl.RelativeTimeFormat("es", { numeric: "auto" });

  // Server clock offset (set by the store from Snapshot.now).
  O.clockOffset = 0;
  O.now = () => Date.now() + O.clockOffset;

  const ms = O.u.ms;
  O.fmt = {
    num: (n) => (Number.isFinite(n) ? NF.format(n) : "—"),
    num1: (n) => (Number.isFinite(n) ? NF1.format(n) : "—"),
    pct: (n) => (Number.isFinite(n) ? NF0.format(n * 100) + " %" : "—"),
    usd(n) {
      if (!Number.isFinite(n)) return "—";
      return Math.abs(n) < 1 ? USD4.format(n) : USD2.format(n);
    },
    tokens(n) {
      if (!Number.isFinite(n) || n <= 0) return "0";
      if (n < 1000) return NF.format(n);
      if (n < 1e6) return NF1.format(n / 1000) + " k";
      return NF1.format(n / 1e6) + " M";
    },
    bytes(n) {
      if (!Number.isFinite(n)) return "—";
      if (n < 1024) return NF.format(n) + " B";
      if (n < 1048576) return NF1.format(n / 1024) + " KiB";
      return NF1.format(n / 1048576) + " MiB";
    },
    dateTime(t) {
      const v = ms(t);
      if (!Number.isFinite(v)) return "—";
      const d = new Date(v);
      return (d.getFullYear() === new Date(O.now()).getFullYear() ? DT : DTY).format(d);
    },
    full(t) {
      const v = ms(t);
      return Number.isFinite(v) ? DFULL.format(new Date(v)) : "";
    },
    day(t) {
      const v = ms(t);
      return Number.isFinite(v) ? DAY.format(new Date(v)) : "—";
    },
    time(t) {
      const v = ms(t);
      return Number.isFinite(v) ? TM.format(new Date(v)) : "—";
    },
    timeSec(t) {
      const v = ms(t);
      return Number.isFinite(v) ? TMS.format(new Date(v)) : "";
    },
    utcTime(t) {
      const v = ms(t);
      return Number.isFinite(v) ? UTC_TM.format(new Date(v)) + " UTC" : "—";
    },
    utcDateTime(t) {
      const v = ms(t);
      return Number.isFinite(v) ? UTC_DT.format(new Date(v)) + " UTC" : "—";
    },
    rel(t) {
      const v = ms(t);
      if (!Number.isFinite(v)) return "—";
      const s = Math.round((v - O.now()) / 1000);
      const a = Math.abs(s);
      if (a < 45) return s <= 0 ? "ahora mismo" : "en unos segundos";
      if (a < 3600) return RTF.format(Math.round(s / 60), "minute");
      if (a < 86400) return RTF.format(Math.round(s / 3600), "hour");
      if (a < 86400 * 30) return RTF.format(Math.round(s / 86400), "day");
      return O.fmt.dateTime(v);
    },
    duration(msv) {
      if (!Number.isFinite(msv) || msv < 0) return "—";
      const s = Math.floor(msv / 1000);
      if (s < 1) return "<1 s";
      if (s < 60) return s + " s";
      const m = Math.floor(s / 60);
      if (m < 60) return m + " min " + String(s % 60).padStart(2, "0") + " s";
      const h = Math.floor(m / 60);
      return h + " h " + String(m % 60).padStart(2, "0") + " min";
    },
    /** Duration between two times, or until now when end is empty. */
    span(start, end) {
      const a = ms(start);
      if (!Number.isFinite(a)) return "—";
      const b = Number.isFinite(ms(end)) ? ms(end) : O.now();
      return O.fmt.duration(b - a);
    },
  };

  // ------------------------------------------------------------- labels --

  O.L = {
    kinds: {
      pregunta: { label: "Pregunta", icon: "❓", hint: "Analiza el repositorio y responde citando ficheros. No cambia nada." },
      plan: { label: "Plan", icon: "🗺️", hint: "Diseña un plan numerado con ficheros afectados, riesgos y cómo verificarlo." },
      cambio: { label: "Cambio", icon: "✏️", hint: "Modifica el código en el sandbox y lo verifica. La Oficina recoge el diff para que lo revises o abras un PR." },
      revision: { label: "Revisión", icon: "🔍", hint: "Revisa cambios o un PR con rigor y termina con un veredicto: aprobado o cambios." },
      retro: { label: "Retro del Coach", icon: "🧭", hint: "El Coach estudia el historial de un agente y propone un system prompt mejor." },
      juez: { label: "Juez", icon: "⚖️", hint: "Puntúa de 0 a 10 el resultado de otro trabajo según unos criterios." },
    },
    status: {
      en_cola: "En cola", preparando: "Preparando", en_curso: "En curso", limpiando: "Recogiendo",
      hecho: "Hecho", fallido: "Fallido", cancelado: "Cancelado", pendiente: "Pendiente",
      aprobada: "Aprobada", rechazada: "Rechazada",
    },
    modes: {
      lectura: { label: "Lectura", hint: "Solo lee y propone: no ejecuta comandos que cambien nada ni edita ficheros." },
      completo: { label: "Completo", hint: "Ejecuta comandos y edita ficheros dentro del sandbox aislado (gVisor). Necesario para los cambios." },
    },
    priority: { 0: "Baja", 1: "Normal", 2: "Alta" },
    verdict: { aprobado: "Aprobado", cambios: "Cambios pedidos" },
    harness: { claude: "Claude Code", codex: "Codex" },
    source: {
      manual: "a mano", issue: "issue", pr: "PR", job: "trabajo", pipeline: "equipo",
      schedule: "turno", coach: "Coach", eval: "evaluación",
    },
    days: ["Domingo", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado"],
    daysShort: ["D", "L", "M", "X", "J", "V", "S"],
  };
  O.isRunning = (st) => st === "preparando" || st === "en_curso" || st === "limpiando";
  O.isFinished = (st) => st === "hecho" || st === "fallido" || st === "cancelado";

  // ---------------------------------------------------------------- API --

  class ApiError extends Error {
    constructor(message, status, field) {
      super(message);
      this.status = status;
      this.field = field || "";
    }
  }
  O.ApiError = ApiError;

  function httpMessage(code) {
    switch (code) {
      case 401: return "La sesión caducó: recarga la página para identificarte de nuevo.";
      case 403: return "La Oficina rechazó la petición.";
      case 404: return "No existe (puede que se haya borrado).";
      case 409: return "No se puede hacer ahora mismo (conflicto).";
      case 413: return "La petición es demasiado grande.";
      case 429: return "Demasiadas peticiones seguidas; espera unos segundos.";
      case 502: case 503: case 504: return "El servidor no responde ahora mismo.";
      default: return "Error " + code + " del servidor.";
    }
  }

  // At most a few requests in flight: the edge limits the whole host.
  const MAX_INFLIGHT = 4;
  let inflight = 0;
  const waiting = [];
  function acquire() {
    if (inflight < MAX_INFLIGHT) {
      inflight++;
      return Promise.resolve();
    }
    return new Promise((resolve) => waiting.push(resolve)).then(() => { inflight++; });
  }
  function release() {
    inflight--;
    const next = waiting.shift();
    if (next) next();
  }

  async function request(method, path, body, asText) {
    await acquire();
    try {
      const init = { method, credentials: "same-origin", cache: "no-store", headers: {} };
      if (method === "POST") {
        init.headers["Content-Type"] = "application/json";
        init.headers["X-AX-Web"] = "1";
        init.body = JSON.stringify(body === undefined || body === null ? {} : body);
      } else {
        init.headers.Accept = asText ? "text/plain" : "application/json";
      }
      let resp;
      try {
        resp = await fetch(path, init);
      } catch (e) {
        throw new ApiError("No se pudo contactar con la Oficina. ¿Hay conexión?", 0);
      }
      const text = await resp.text().catch(() => "");
      if (!resp.ok) {
        let msg = "", field = "";
        try {
          const d = JSON.parse(text);
          msg = d && typeof d.error === "string" ? d.error : "";
          field = d && typeof d.field === "string" ? d.field : "";
        } catch (e) { /* not JSON */ }
        throw new ApiError(msg || httpMessage(resp.status), resp.status, field);
      }
      if (asText) return text;
      if (!text) return {};
      try {
        return JSON.parse(text);
      } catch (e) {
        throw new ApiError("La Oficina respondió algo que no es JSON.", resp.status);
      }
    } finally {
      release();
    }
  }

  O.api = {
    get: (path) => request("GET", path),
    text: (path) => request("GET", path, null, true),
    post: (path, body) => request("POST", path, body),
  };
  O.enc = encodeURIComponent;

  // -------------------------------------------------------------- icons --

  // 24x24 stroke icons drawn from primitives: [tag, attrs].
  const P = (d) => ["path", { d }];
  const C = (cx, cy, r, fill) => ["circle", { cx, cy, r, class: fill ? "ic-fill" : null }];
  const R = (x, y, w, h, rx) => ["rect", { x, y, width: w, height: h, rx: rx || 0 }];
  const ICONS = {
    office: [R(4, 3, 16, 18, 2), P("M9 7h1M14 7h1M9 11h1M14 11h1M9 15h1M14 15h1"), P("M10 21v-3h4v3")],
    inbox: [P("M3 13l2.6-7.4A2 2 0 0 1 7.5 4h9a2 2 0 0 1 1.9 1.6L21 13v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"), P("M3 13h5l1.5 2.5h5L16 13h5")],
    board: [R(3, 4, 18, 16, 2), P("M9 4v16M15 4v16")],
    plus: [P("M12 5v14M5 12h14")],
    team: [C(9, 8, 3), C(17, 9, 2.5), P("M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"), P("M15.5 14.2c3 .2 5.5 2.2 5.5 5.3")],
    agent: [R(5, 8, 14, 11, 3), P("M12 4v4"), C(12, 3.5, 1), C(9.5, 13, 1, true), C(14.5, 13, 1, true), P("M9.5 16.3h5")],
    folder: [P("M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z")],
    clock: [C(12, 12, 9), P("M12 7v5l3 2")],
    trend: [P("M3 17l6-6 4 4 8-8"), P("M15 7h6v6")],
    server: [R(4, 4, 16, 7, 2), R(4, 13, 16, 7, 2), P("M8 7.5h.01M8 16.5h.01M12 7.5h4M12 16.5h4")],
    cube: [P("M12 3l8 4.5v9L12 21l-8-4.5v-9z"), P("M12 12l8-4.5M12 12L4 7.5M12 12v9")],
    sliders: [P("M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"), C(15, 6, 2), C(9, 12, 2), C(17, 18, 2)],
    more: [C(5, 12, 1.4, true), C(12, 12, 1.4, true), C(19, 12, 1.4, true)],
    close: [P("M6 6l12 12M18 6L6 18")],
    pause: [P("M9 5v14M15 5v14")],
    play: [P("M7 4.5l12 7.5-12 7.5z")],
    check: [P("M5 12.5l4.5 4.5L19 7")],
    alert: [P("M12 3.5l9.5 16.5h-19z"), P("M12 10v4M12 17h.01")],
    external: [P("M14 4h6v6M20 4l-9 9"), P("M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5")],
    download: [P("M12 4v12M7 11l5 5 5-5M5 20h14")],
    refresh: [P("M20 11a8 8 0 1 0-2.3 5.7"), P("M20 4v7h-7")],
    trash: [P("M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3")],
    edit: [P("M4 20h4L19 9l-4-4L4 16z"), P("M13.5 6.5l4 4")],
    chevronRight: [P("M9 6l6 6-6 6")],
    chevronDown: [P("M6 9l6 6 6-6")],
    back: [P("M19 12H5M11 6l-6 6 6 6")],
    branch: [C(6, 6, 2), C(6, 18, 2), C(18, 8, 2), P("M6 8v8M18 10c0 4-6 3.5-10.5 6.5")],
    send: [P("M4 12l16-8-6 16-3-7z")],
    history: [P("M3 12a9 9 0 1 0 3-6.7"), P("M3 4v5h5"), P("M12 8v4l3 2")],
    sparkle: [P("M12 3l1.8 5.6L19.5 10l-5.7 1.6L12 17l-1.8-5.4L4.5 10l5.7-1.4z"), P("M19 16l.7 2 2 .7-2 .6-.7 2-.6-2-2-.6 2-.7z")],
    copy: [R(8, 8, 12, 12, 2), P("M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2")],
    calendar: [R(3, 5, 18, 16, 2), P("M3 10h18M8 3v4M16 3v4")],
    flask: [P("M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"), P("M7.5 15h9")],
    link: [P("M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"), P("M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1")],
    filter: [P("M4 5h16l-6 8v6l-4-2v-4z")],
    key: [C(8, 15, 4), P("M11 12l9-9M16 7l3 3")],
    keyboard: [R(2, 6, 20, 12, 2), P("M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10")],
    stop: [R(6, 6, 12, 12, 2)],
    eye: [P("M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"), C(12, 12, 3)],
    archive: [R(3, 4, 18, 5, 1), P("M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4")],
    undo: [P("M9 14L4 9l5-5"), P("M4 9h10a6 6 0 0 1 0 12h-2")],
    menu: [P("M4 7h16M4 12h16M4 17h16")],
    live: [C(12, 12, 3, true), P("M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14")],
  };

  /** icon(name, {label, size, cls}) returns an SVG icon. */
  O.icon = function icon(name, opts) {
    const o = opts || {};
    const spec = ICONS[name] || ICONS.more;
    const el = O.s("svg", {
      class: ["ic", o.cls], viewBox: "0 0 24 24", width: o.size || 18, height: o.size || 18,
      "aria-hidden": o.label ? null : "true", role: o.label ? "img" : null, "aria-label": o.label || null,
      focusable: "false",
    });
    for (const [tag, attrs] of spec) el.appendChild(O.s(tag, attrs));
    return el;
  };

  // ------------------------------------------------------------- toasts --

  const TOAST_ICON = { info: "ℹ️", ok: "✅", error: "⛔", warn: "⚠️" };
  O.toast = function toast(message, opts) {
    const o = opts || {};
    const root = document.getElementById("toasts");
    if (!root) return () => {};
    const kind = o.kind || "info";
    let timer = null;
    const close = () => {
      clearTimeout(timer);
      t.classList.add("is-leaving");
      setTimeout(() => t.remove(), 220);
    };
    const t = O.h("div", { class: ["toast", "toast-" + kind], role: kind === "error" ? "alert" : "status" },
      O.h("span", { class: "toast-icon", "aria-hidden": "true", text: TOAST_ICON[kind] || "" }),
      O.h("div", { class: "toast-body" },
        o.title ? O.h("strong", { class: "toast-title", text: o.title }) : null,
        O.h("span", { text: message })),
      o.action ? O.h("button", {
        class: "btn btn-sm btn-ghost", type: "button", text: o.action.label,
        on: { click: () => { close(); o.action.fn(); } },
      }) : null,
      O.h("button", { class: "toast-close", type: "button", "aria-label": "Cerrar aviso", on: { click: close } },
        O.icon("close", { size: 16 })));
    root.appendChild(t);
    while (root.children.length > 4) root.firstElementChild.remove();
    timer = setTimeout(close, o.timeout || (kind === "error" ? 9000 : 4500));
    return close;
  };
  O.fail = (err, title) => {
    const msg = err && err.message ? err.message : String(err);
    O.toast(msg, { kind: "error", title });
  };

  // ------------------------------------------------------------ dialogs --

  /**
   * dialog({title, body, actions:[{label, kind, onClick}], wide}) opens a
   * modal. onClick may return false (or a promise of false) to keep it
   * open. Returns {close(value), body, el}.
   */
  O.dialog = function dialog(opts) {
    const id = O.u.uid("dlg");
    let result;
    const bodyEl = O.h("div", { class: "dialog-body" }, opts.body);
    const footer = O.h("div", { class: "dialog-actions" });
    const inner = O.h("div", { class: "dialog-inner" },
      O.h("div", { class: "dialog-head" },
        O.h("h2", { id, class: "dialog-title", text: opts.title || "" }),
        O.h("button", { class: "btn-icon", type: "button", "aria-label": "Cerrar", on: { click: () => api.close() } },
          O.icon("close"))),
      bodyEl, footer);
    const dlg = O.h("dialog", { class: ["dialog", opts.wide && "dialog-wide"], "aria-labelledby": id }, inner);
    const api = {
      el: dlg,
      body: bodyEl,
      close(v) {
        result = v;
        if (typeof dlg.close === "function" && dlg.open) dlg.close();
        else finish();
      },
    };
    let done = false;
    function finish() {
      if (done) return;
      done = true;
      dlg.remove();
      if (opts.onClose) opts.onClose(result);
    }
    for (const a of opts.actions || []) {
      const b = O.h("button", {
        type: "button", class: ["btn", a.kind ? "btn-" + a.kind : null], text: a.label,
        on: {
          click: async () => {
            if (!a.onClick) return api.close();
            b.disabled = true;
            try {
              const r = await a.onClick(api);
              if (r !== false && !done && dlg.open) api.close(r);
            } catch (e) {
              O.fail(e);
            } finally {
              b.disabled = false;
            }
          },
        },
      });
      footer.appendChild(b);
    }
    dlg.addEventListener("close", finish);
    dlg.addEventListener("click", (e) => { if (e.target === dlg) api.close(); });
    document.body.appendChild(dlg);
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
    const focus = dlg.querySelector("[autofocus]") || dlg.querySelector("textarea, input, select");
    if (focus) focus.focus();
    return api;
  };

  /**
   * confirm({title, text, confirmLabel, danger, typeToConfirm}) resolves to
   * true (or the typed text) when confirmed, false otherwise.
   */
  O.confirm = function confirm(opts) {
    return new Promise((resolve) => {
      let input = null;
      const err = O.h("p", { class: "field-error", role: "alert" });
      const body = [];
      for (const p of [].concat(opts.text || [])) body.push(typeof p === "string" ? O.h("p", { text: p }) : p);
      if (opts.typeToConfirm) {
        const fid = O.u.uid("confirm");
        input = O.h("input", { id: fid, class: "input mono", autocomplete: "off", spellcheck: "false", autofocus: true });
        body.push(O.h("label", { class: "field-label", for: fid },
          "Escribe ", O.h("code", { text: opts.typeToConfirm }), " para confirmar"), input, err);
      }
      O.dialog({
        title: opts.title || "¿Seguro?",
        body,
        actions: [
          { label: opts.cancelLabel || "Cancelar", kind: "ghost", onClick: () => undefined },
          {
            label: opts.confirmLabel || "Confirmar", kind: opts.danger ? "danger" : "primary",
            onClick: () => {
              if (input && input.value !== opts.typeToConfirm) {
                err.textContent = "El texto no coincide.";
                input.focus();
                return false;
              }
              return input ? input.value : true;
            },
          },
        ],
        onClose: (v) => resolve(v || false),
      });
    });
  };

  /** Runs fn, showing a toast on failure; returns its result or undefined. */
  O.attempt = async (fn, title) => {
    try {
      return await fn();
    } catch (e) {
      O.fail(e, title);
      return undefined;
    }
  };
})();
