/*
 * Oficina de agentes · safe Markdown.
 *
 * parse(text) turns Markdown into a small tree of plain objects; render()
 * builds DOM from it with createElement and text nodes only, so nothing in
 * the input is ever interpreted as markup (escape by construction). Raw
 * HTML stays visible as text. Links keep only http(s) URLs without
 * credentials, always open in a new tab with rel="noopener noreferrer" and
 * show their host. Images become links. Invisible and control characters
 * are drawn as ⟦U+XXXX⟧ markers (O.visible).
 *
 * Agent output is hostile input of up to 256 KiB, so every step is linear:
 * block syntax is recognised by hand-written scanners (no regex with
 * overlapping runs), lines longer than MAX_BLOCK_LINE are plain paragraph
 * text, link scans are bounded and share a work budget per inline run, and
 * inputs over MAX_AUTO_BYTES render as plain text until the user asks.
 * After MAX_NODES elements the rest of the input is plain text, which also
 * bounds the size of the DOM.
 *
 * Supported: ATX headings, paragraphs, emphasis, strong, strikethrough,
 * code spans, fenced code, block quotes, (nested, ordered, task) lists,
 * thematic breaks, GFM tables, links, autolinks and bare URLs.
 */
(() => {
  "use strict";
  const O = window.Oficina;

  const MAX_DEPTH = 8;          // nesting of quotes and lists
  const MAX_INLINE_DEPTH = 6;   // nesting of emphasis and links
  const MAX_EMPHASIS_LEN = 20000;
  const MAX_BLOCK_LINE = 2000;  // longer lines never start a block
  const MAX_LINK_TEXT = 2000;   // how far a "[" looks for its "]"
  const MAX_DEST_LEN = 2048;    // how far "(" looks for its ")" (with title)
  const MAX_URL_LEN = 2048;     // longer bare URLs stay plain text
  const MAX_AUTO_BYTES = 65536; // bigger inputs render as text until asked
  const MAX_NODES = 40000;      // elements per parse; the rest stays text
  const PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";
  const URL_TRAIL = ".,:;!?'\"*_~";

  // ------------------------------------------------------------- blocks --

  const isST = (ch) => ch === " " || ch === "\t";
  const isBlank = (l) => /^[ \t]*$/.test(l);
  const short = (l) => l.length <= MAX_BLOCK_LINE;

  /** s without leading spaces and tabs. */
  function trimStartST(s) {
    let a = 0;
    while (a < s.length && isST(s[a])) a++;
    return a ? s.slice(a) : s;
  }

  /** s without trailing spaces and tabs. */
  function trimEndST(s) {
    let b = s.length;
    while (b > 0 && isST(s[b - 1])) b--;
    return b < s.length ? s.slice(0, b) : s;
  }

  const trimST = (s) => trimEndST(trimStartST(s));

  function indentOf(line) {
    let col = 0;
    for (const ch of line) {
      if (ch === " ") col++;
      else if (ch === "\t") col += 4 - (col % 4);
      else break;
    }
    return col;
  }

  // Removes up to n columns of leading whitespace.
  function dedent(line, n) {
    let col = 0, i = 0;
    while (i < line.length && col < n) {
      const ch = line[i];
      if (ch === " ") col++;
      else if (ch === "\t") col += 4 - (col % 4);
      else break;
      i++;
    }
    return (col > n ? " ".repeat(col - n) : "") + line.slice(i);
  }

  function sanitizeLang(s) {
    const m = /^[A-Za-z0-9_+#.-]{1,32}/.exec(s || "");
    return m ? m[0].toLowerCase().replace(/[^a-z0-9-]/g, "-") : "";
  }

  // Up to three leading spaces: the index of the first other character.
  function lead(line) {
    let i = 0;
    while (i < 3 && line[i] === " ") i++;
    return i;
  }

  /** Fence opener: {indent, ch, len, lang} or null. */
  function fenceOf(line) {
    if (!short(line)) return null;
    const i = lead(line);
    const ch = line[i];
    if (ch !== "`" && ch !== "~") return null;
    let j = i;
    while (line[j] === ch) j++;
    if (j - i < 3) return null;
    const info = line.slice(j);
    if (info.includes("`")) return null;
    return { indent: i, ch, len: j - i, lang: sanitizeLang(/^[ \t]*([^\s`]*)/.exec(info)[1]) };
  }

  /** Closing fence of f: at least as many fence characters, then blanks. */
  function closesFence(line, f) {
    const i = lead(line);
    let j = i;
    while (line[j] === f.ch) j++;
    if (j - i < f.len) return false;
    for (; j < line.length; j++) if (!isST(line[j])) return false;
    return true;
  }

  /** ATX heading: {level, text} or null. */
  function headingOf(line) {
    if (!short(line)) return null;
    const i = lead(line);
    let j = i;
    while (line[j] === "#" && j - i < 7) j++;
    const level = j - i;
    if (level < 1 || level > 6) return null;
    if (j < line.length && !isST(line[j])) return null;
    let a = j, b = line.length;
    while (a < b && isST(line[a])) a++;
    while (b > a && isST(line[b - 1])) b--;
    // Optional closing sequence: "#"s preceded by a blank (or alone).
    let k = b;
    while (k > a && line[k - 1] === "#") k--;
    if (k < b && (k === a || isST(line[k - 1]))) {
      b = k;
      while (b > a && isST(line[b - 1])) b--;
    }
    return { level, text: line.slice(a, b) };
  }

  /** Thematic break: three or more "-", "*" or "_" with optional blanks. */
  function isHr(line) {
    if (!short(line)) return false;
    let i = lead(line);
    const ch = line[i];
    if (ch !== "-" && ch !== "*" && ch !== "_") return false;
    let n = 0;
    for (; i < line.length; i++) {
      if (line[i] === ch) n++;
      else if (!isST(line[i])) return false;
    }
    return n >= 3;
  }

  /** Block quote line: its content after ">" (and one space), or null. */
  function quoteOf(line) {
    if (!short(line)) return null;
    let i = lead(line);
    if (line[i] !== ">") return null;
    i++;
    if (line[i] === " ") i++;
    return line.slice(i);
  }

  // [indent, marker, spaces after the marker, content]; anchored and
  // without overlapping runs ([\s\S]* always reaches the end).
  const RE_LIST = /^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)([\s\S]*)$/;
  const listOf = (line) => (short(line) ? RE_LIST.exec(line) : null);

  /** GFM delimiter row: cells of :?-+:? between optional outer pipes. */
  function isTableDelim(line) {
    if (!short(line) || !line.includes("-")) return false;
    let s = trimST(line);
    if (s[0] === "|") s = s.slice(1);
    if (s[s.length - 1] === "|") s = s.slice(0, -1);
    for (const cell of s.split("|")) if (!/^:?-+:?$/.test(trimST(cell))) return false;
    return true;
  }

  const startsTable = (lines, i) => lines[i].includes("|") && short(lines[i]) && i + 1 < lines.length && isTableDelim(lines[i + 1]);

  // A line that starts a block other than a paragraph (paragraphs end there).
  function startsBlock(line) {
    if (!short(line)) return false;
    if (fenceOf(line) || headingOf(line) || isHr(line) || quoteOf(line) !== null) return true;
    const m = listOf(line);
    if (m && m[4].trim() !== "") {
      // An ordered list interrupts a paragraph only when it starts at 1.
      if (/^\d/.test(m[2])) return /^1[.)]$/.test(m[2]);
      return true;
    }
    return false;
  }

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const cells = [];
    let cur = "", inCode = false;
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (ch === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
      if (ch === "`") inCode = !inCode;
      if (ch === "|" && !inCode) { cells.push(cur.trim()); cur = ""; continue; }
      cur += ch;
    }
    cells.push(cur.trim());
    return cells;
  }

  // Elements the current parse may still create (reset by parse()).
  let nodesLeft = MAX_NODES;

  function parseBlocks(lines, depth) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      if (nodesLeft <= 0) {
        out.push({ t: "p", rest: true, c: [{ t: "text", v: lines.slice(i).join("\n") }] });
        break;
      }
      nodesLeft--;

      if (short(line)) {
        const f = fenceOf(line);
        if (f) {
          const body = [];
          i++;
          while (i < lines.length && !closesFence(lines[i], f)) {
            body.push(dedent(lines[i], f.indent));
            i++;
          }
          i++; // closing fence (or end of input)
          out.push({ t: "code", lang: f.lang, text: body.join("\n") });
          continue;
        }

        const hd = headingOf(line);
        if (hd) {
          out.push({ t: "h", level: hd.level, c: parseInline(hd.text, 0) });
          i++;
          continue;
        }

        if (isHr(line)) {
          out.push({ t: "hr" });
          i++;
          continue;
        }

        if (quoteOf(line) !== null && depth < MAX_DEPTH) {
          const body = [];
          while (i < lines.length && !isBlank(lines[i])) {
            const q = quoteOf(lines[i]);
            if (q !== null) body.push(q);
            else if (!startsBlock(lines[i])) body.push(lines[i]); // lazy continuation
            else break;
            i++;
          }
          out.push({ t: "quote", c: parseBlocks(body, depth + 1) });
          continue;
        }

        if (listOf(line) && depth < MAX_DEPTH) {
          const r = parseList(lines, i, depth);
          out.push(r.node);
          i = r.next;
          continue;
        }

        // GFM table: header row, delimiter row, body rows.
        if (startsTable(lines, i)) {
          const head = splitRow(line);
          const delim = splitRow(lines[i + 1]);
          if (head.length === delim.length) {
            const align = delim.map((d) => {
              const l = d.startsWith(":"), r = d.endsWith(":");
              return l && r ? "c" : r ? "r" : l ? "l" : "";
            });
            const rows = [];
            i += 2;
            while (i < lines.length && nodesLeft > 0 && !isBlank(lines[i]) && lines[i].includes("|") && !startsBlock(lines[i])) {
              const cells = splitRow(lines[i]);
              const row = [];
              for (let k = 0; k < head.length; k++) row.push(parseInline(cells[k] || "", 0));
              rows.push(row);
              nodesLeft -= row.length + 1;
              i++;
            }
            out.push({ t: "table", align, head: head.map((c) => parseInline(c, 0)), rows });
            continue;
          }
        }
      }

      // Paragraph (every line longer than MAX_BLOCK_LINE ends up here).
      const para = [trimStartST(line)];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) {
        // A table starting right after paragraph text.
        if (startsTable(lines, i)) break;
        para.push(trimStartST(lines[i]));
        i++;
      }
      out.push({ t: "p", c: parseInline(trimEndST(para.join("\n")), 0) });
    }
    return out;
  }

  function parseList(lines, start, depth) {
    const first = listOf(lines[start]);
    const ordered = /^\d/.test(first[2]);
    const delim = first[2].slice(-1);
    const baseIndent = first[1].length;
    const node = { t: ordered ? "ol" : "ul", start: ordered ? parseInt(first[2], 10) : 1, loose: false, items: [] };
    let i = start;
    while (i < lines.length && (nodesLeft > 0 || i === start)) {
      const m = listOf(lines[i]);
      if (!m) break;
      const isOrd = /^\d/.test(m[2]);
      if (isOrd !== ordered || m[2].slice(-1) !== delim || m[1].length > baseIndent + 1) break;
      const spaces = m[3].length;
      const contentIndent = m[1].length + m[2].length + (spaces >= 1 && spaces <= 4 ? spaces : 1);
      const body = [m[4]];
      i++;
      while (i < lines.length) {
        const l = lines[i];
        if (isBlank(l)) {
          let j = i + 1;
          while (j < lines.length && isBlank(lines[j])) j++;
          if (j < lines.length && indentOf(lines[j]) >= contentIndent) {
            for (; i < j; i++) body.push("");
            continue;
          }
          break;
        }
        if (indentOf(l) >= contentIndent) {
          body.push(dedent(l, contentIndent));
          i++;
          continue;
        }
        // Lazy continuation of the item's paragraph.
        if (!listOf(l) && !startsBlock(l) && !isBlank(body[body.length - 1])) {
          body.push(l.trim());
          i++;
          continue;
        }
        break;
      }
      let task = null;
      const tm = /^\[([ xX])\](?:[ \t]+|$)/.exec(body[0]);
      if (tm) {
        task = tm[1] !== " ";
        body[0] = body[0].slice(tm[0].length);
      }
      nodesLeft--;
      node.items.push({ task, c: parseBlocks(body, depth + 1) });
      // Blank lines between items make the list loose.
      if (i < lines.length && isBlank(lines[i])) {
        let j = i;
        while (j < lines.length && isBlank(lines[j])) j++;
        const n = j < lines.length ? listOf(lines[j]) : null;
        if (n && /^\d/.test(n[2]) === ordered && n[2].slice(-1) === delim && n[1].length <= baseIndent + 1) {
          node.loose = true;
          i = j;
          continue;
        }
        break;
      }
    }
    return { node, next: i };
  }

  // ------------------------------------------------------------- inline --

  /*
   * One inline run (a paragraph, a heading, a table cell, the inside of an
   * emphasis) is parsed with a context: the string, a work budget for the
   * link scans (fuel: when it runs out, the rest of the run has no links)
   * and, built on first use, the positions of its backtick runs by length.
   */
  function inlineCtx(s) {
    return { s, fuel: 8 * s.length + 1024, ticks: null, lastMid: s.lastIndexOf("](") };
  }

  function tickRuns(ctx) {
    if (ctx.ticks) return ctx.ticks;
    const s = ctx.s, runs = new Map();
    let i = s.indexOf("`");
    while (i >= 0) {
      let j = i + 1;
      while (s[j] === "`") j++;
      let a = runs.get(j - i);
      if (!a) runs.set(j - i, (a = []));
      a.push(i);
      i = s.indexOf("`", j);
    }
    ctx.ticks = runs;
    return runs;
  }

  /**
   * Returns the index of the next run of exactly n backticks at or after
   * from, or -1. from is always just past a run, so runs start at their
   * first backtick. Binary search: no rescans of the string.
   */
  function findTicks(ctx, from, n) {
    const a = tickRuns(ctx).get(n);
    if (!a) return -1;
    let lo = 0, hi = a.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (a[mid] < from) lo = mid + 1;
      else hi = mid;
    }
    return lo < a.length ? a[lo] : -1;
  }

  /** Destination of a link: "(url "title")" starting at s[i] === "(". */
  function linkDest(ctx, i) {
    const s = ctx.s;
    const end = Math.min(s.length, i + MAX_DEST_LEN);
    let j = i + 1;
    const done = (r) => {
      ctx.fuel -= j - i;
      return r;
    };
    while (j < end && (s[j] === " " || s[j] === "\n")) j++;
    let url = "";
    if (s[j] === "<") {
      let k = j + 1;
      while (k < end && s[k] !== ">" && s[k] !== "\n") k++;
      if (k >= end || s[k] !== ">") {
        j = k;
        return done(null);
      }
      url = s.slice(j + 1, k);
      j = k + 1;
    } else {
      let depth = 0;
      const st = j;
      while (j < end) {
        const ch = s[j];
        if (ch === "\\" && j + 1 < s.length) { j += 2; continue; }
        if (ch === "(") depth++;
        else if (ch === ")") { if (depth === 0) break; depth--; }
        else if (ch === " " || ch === "\n" || ch < " ") break;
        j++;
      }
      if (j >= end) return done(null);
      url = s.slice(st, j).replace(/\\([!-/:-@[-`{-~])/g, "$1");
    }
    while (j < end && (s[j] === " " || s[j] === "\n")) j++;
    if (j < end && (s[j] === "\"" || s[j] === "'")) {
      const q = s[j];
      let k = j + 1;
      while (k < end && s[k] !== q) k++;
      if (k >= end) {
        j = k;
        return done(null);
      }
      j = k + 1;
      while (j < end && (s[j] === " " || s[j] === "\n")) j++;
    }
    if (j >= end || s[j] !== ")") return done(null);
    return done({ url, end: j + 1 });
  }

  /** [text](dest) starting at s[i] === "[". */
  function parseLink(ctx, i) {
    const s = ctx.s;
    // No "](" after i (or no budget left): it cannot be a link.
    if (ctx.fuel <= 0 || ctx.lastMid < i) return null;
    let depth = 0, j = i;
    const limit = Math.min(s.length, i + MAX_LINK_TEXT);
    for (; j < limit; j++) {
      const ch = s[j];
      if (ch === "\\") { j++; continue; }
      if (ch === "`") {
        let run = 1;
        while (j + run < limit && s[j + run] === "`") run++;
        if (j + run >= limit) { j = limit; break; }
        const k = findTicks(ctx, j + run, run);
        if (k >= 0 && k < limit) { j = k + run - 1; continue; }
        j += run - 1;
        continue;
      }
      if (ch === "[") depth++;
      else if (ch === "]") {
        depth--;
        if (depth === 0) break;
      }
    }
    ctx.fuel -= j - i + 1;
    if (j >= limit || s[j] !== "]" || s[j + 1] !== "(") return null;
    const d = linkDest(ctx, j + 1);
    if (!d) return null;
    return { text: s.slice(i + 1, j), url: d.url, end: d.end };
  }

  /**
   * Keeps http(s) URLs without credentials; returns the normalized URL or
   * null.
   */
  function safeHref(raw) {
    const s = String(raw || "").trim();
    if (!/^https?:\/\//i.test(s)) return null;
    if (/[\u0000- \u007f]/.test(s)) return null;
    try {
      const u = new URL(s);
      if (u.protocol !== "http:" && u.protocol !== "https:") return null;
      if (!u.hostname || u.username || u.password) return null;
      // The URL parser keeps "\\" in queries and fragments; O.safeLink (and
      // so h()) refuses it anywhere, so it is percent-encoded here.
      return u.href.replace(/\\/g, "%5C");
    } catch (e) {
      return null;
    }
  }

  function hostOf(href) {
    try {
      return new URL(href).hostname;
    } catch (e) {
      return "";
    }
  }

  // Link texts that read as an address: a scheme, "www.", a host followed
  // by a path, or a bare host with a common top-level domain.
  const RE_TEXT_SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;
  const RE_TEXT_HOST = /^(?:[\p{L}\p{N}_-]+\.)+[\p{L}\p{N}-]+(?::\d{1,5})?(?=[/?#]|$)/u;
  const COMMON_TLD = new Set(["com", "org", "net", "io", "dev", "app", "ai", "co", "es", "eu", "gov", "edu",
    "info", "me", "uk", "de", "fr", "it", "pt", "us", "cloud", "tech", "xyz", "online", "site", "page", "biz"]);

  /** The host a link text claims to be, or "" when it is not an address. */
  function claimedHost(text) {
    const t = text.trim();
    if (t.length < 4 || t.length > MAX_URL_LEN || /\s/.test(t)) return "";
    let candidate = "";
    if (RE_TEXT_SCHEME.test(t)) candidate = t;
    else {
      const m = RE_TEXT_HOST.exec(t);
      if (!m) return "";
      const host = m[0].replace(/:\d+$/, "").toLowerCase();
      const tld = host.slice(host.lastIndexOf(".") + 1);
      if (/^www\./.test(host) || t.length > m[0].length || COMMON_TLD.has(tld)) candidate = "http://" + t;
      else return "";
    }
    return hostOf(candidate);
  }

  /** Plain text of inline nodes (what a link shows). */
  function plainText(nodes) {
    let s = "";
    for (const n of nodes) {
      if (n.t === "text" || n.t === "code") s += n.v;
      else if (n.c) s += plainText(n.c);
    }
    return s;
  }

  // Trims trailing punctuation from a bare URL (keeping balanced parens).
  // The parens are counted once; trimming only updates the counters.
  function trimUrl(u) {
    let open = 0, close = 0;
    for (let k = 0; k < u.length; k++) {
      if (u[k] === "(") open++;
      else if (u[k] === ")") close++;
    }
    let end = u.length;
    while (end > 0) {
      const last = u[end - 1];
      if (URL_TRAIL.includes(last)) { end--; continue; }
      if (last === ")" && close > open) { close--; end--; continue; }
      break;
    }
    return u.slice(0, end);
  }

  const RE_URL = /https?:\/\/[^\s<>]+/iy;
  const RE_AUTOLINK = /<(https?:\/\/[^\s<>]+)>/iy;
  const isWS = (ch) => ch === undefined || /\s/.test(ch);
  const isAlnum = (ch) => ch !== undefined && /[\p{L}\p{N}]/u.test(ch);

  /**
   * Finds the closing delimiter run for an emphasis opened with len
   * characters ch, from position from. Returns the index where the closing
   * len characters start, or -1. Code spans and escapes are skipped. A
   * failure is remembered: later searches from further on fail at once.
   */
  function findCloser(ctx, from, ch, len, memo) {
    const s = ctx.s;
    const key = ch + len;
    if (memo[key] !== undefined && from >= memo[key]) return -1;
    let i = from;
    while (i < s.length) {
      const c = s[i];
      if (c === "\\") { i += 2; continue; }
      if (c === "`") {
        let run = 1;
        while (s[i + run] === "`") run++;
        const k = findTicks(ctx, i + run, run);
        i = k >= 0 ? k + run : i + run;
        continue;
      }
      if (c === ch) {
        let run = 1;
        while (s[i + run] === ch) run++;
        const ok = i > from && !isWS(s[i - 1]) && (ch !== "_" || !isAlnum(s[i + run]));
        const fits = len === 2 ? run >= 2 : run === 1 || run >= 3;
        if (ok && fits) return i + run - len;
        i += run;
        continue;
      }
      i++;
    }
    memo[key] = memo[key] === undefined ? from : Math.min(memo[key], from);
    return -1;
  }

  function parseInline(src, depth, noLinks) {
    const s = String(src || "");
    const ctx = inlineCtx(s);
    const out = [];
    const memo = {};
    // Plain text so far; spaces are counted apart (sp) so that trailing
    // spaces before a newline are found without rescanning buf.
    let buf = "", sp = 0;
    const put = (t) => {
      if (sp) { buf += " ".repeat(sp); sp = 0; }
      buf += t;
    };
    const flush = () => {
      if (sp) { buf += " ".repeat(sp); sp = 0; }
      if (buf) out.push({ t: "text", v: buf });
      buf = "";
    };
    const emit = (n) => {
      flush();
      nodesLeft--;
      out.push(n);
    };
    const emphasis = depth < MAX_INLINE_DEPTH && s.length <= MAX_EMPHASIS_LEN;
    let i = 0;
    while (i < s.length) {
      if (nodesLeft <= 0) {
        put(s.slice(i));
        break;
      }
      const c = s[i];
      if (c === " ") { sp++; i++; continue; }
      if (c === "\\" && i + 1 < s.length) {
        const d = s[i + 1];
        if (d === "\n") { emit({ t: "br" }); i += 2; continue; }
        if (PUNCT.includes(d)) { put(d); i += 2; continue; }
        put(c);
        i++;
        continue;
      }
      if (c === "`") {
        let run = 1;
        while (s[i + run] === "`") run++;
        const k = findTicks(ctx, i + run, run);
        if (k >= 0) {
          let code = s.slice(i + run, k).replace(/\n/g, " ");
          if (code.length >= 2 && code[0] === " " && code[code.length - 1] === " " && code.trim() !== "") code = code.slice(1, -1);
          emit({ t: "code", v: code });
          i = k + run;
          continue;
        }
        put("`".repeat(run));
        i += run;
        continue;
      }
      if (c === "\n") {
        // Two or more spaces before a newline: hard break.
        if (sp >= 2) {
          sp = 0;
          emit({ t: "br" });
        } else {
          sp = 0;
          buf += "\n";
        }
        i++;
        while (s[i] === " " || s[i] === "\t") i++;
        continue;
      }
      if (depth < MAX_INLINE_DEPTH && !noLinks) {
        if (c === "!" && s[i + 1] === "[") {
          const l = parseLink(ctx, i + 1);
          if (l) {
            emit({ t: "img", alt: l.text, href: safeHref(l.url) });
            i = l.end;
            continue;
          }
        }
        if (c === "[") {
          const l = parseLink(ctx, i);
          if (l) {
            emit({ t: "a", href: safeHref(l.url), c: parseInline(l.text, depth + 1, true) });
            i = l.end;
            continue;
          }
        }
        if (c === "<") {
          RE_AUTOLINK.lastIndex = i;
          const m = RE_AUTOLINK.exec(s);
          if (m) {
            emit({ t: "a", href: m[1].length <= MAX_URL_LEN ? safeHref(m[1]) : null, c: [{ t: "text", v: m[1] }] });
            i += m[0].length;
            continue;
          }
        }
        if ((c === "h" || c === "H") && !isAlnum(s[i - 1])) {
          RE_URL.lastIndex = i;
          const m = RE_URL.exec(s);
          if (m) {
            // Too long to be a real address: keep the whole run as text.
            if (m[0].length > MAX_URL_LEN) {
              put(m[0]);
              i += m[0].length;
              continue;
            }
            const url = trimUrl(m[0]);
            if (url.length > 8) {
              emit({ t: "a", href: safeHref(url), c: [{ t: "text", v: url }] });
              i += url.length;
              continue;
            }
          }
        }
      }
      if (emphasis && (c === "*" || c === "_" || c === "~")) {
        let run = 1;
        while (s[i + run] === c) run++;
        const after = s[i + run];
        const okOpen = !isWS(after) && !(c === "_" && isAlnum(s[i - 1]));
        if (okOpen && c === "~" && run === 2) {
          const k = s.indexOf("~~", i + 2);
          if (k > i + 2 && !isWS(s[k - 1])) {
            emit({ t: "del", c: parseInline(s.slice(i + 2, k), depth + 1, noLinks) });
            i = k + 2;
            continue;
          }
        } else if (okOpen && c !== "~") {
          const len = run >= 2 ? 2 : 1;
          const k = findCloser(ctx, i + len, c, len, memo);
          if (k > i + len) {
            // A longer opening run keeps its extra characters inside.
            emit({ t: len === 2 ? "strong" : "em", c: parseInline(s.slice(i + len, k), depth + 1, noLinks) });
            i = k + len;
            continue;
          }
        }
        put(c.repeat(run));
        i += run;
        continue;
      }
      put(c);
      i++;
    }
    flush();
    return out;
  }

  function tooBig(src) {
    if (src.length <= MAX_AUTO_BYTES / 3) return false;
    return O.u.bytes(src) > MAX_AUTO_BYTES;
  }

  /**
   * parse(text, {full}) returns the block tree. Inputs over 64 KiB come
   * back as one {t: "big"} node (shown as plain text) unless full is set.
   */
  function parse(text, opts) {
    const src = String(text || "").replace(/\r\n?/g, "\n").replace(/\u0000/g, "\uFFFD");
    if (!(opts && opts.full) && tooBig(src)) return [{ t: "big", text: src }];
    nodesLeft = MAX_NODES;
    return parseBlocks(src.split("\n"), 0);
  }

  // ------------------------------------------------------------- render --

  const vis = (s) => O.visible(s);

  function renderLink(n) {
    const h = O.h;
    const host = hostOf(n.href);
    const kids = renderInline(n.c);
    // The text claims another address than the real one: show the real host.
    const claimed = claimedHost(plainText(n.c));
    if (claimed && claimed !== host) kids.push(h("span", { class: "md-href-host", text: " [" + host + "]" }));
    return h("a", { href: n.href, target: "_blank", rel: "noopener noreferrer", title: host }, kids);
  }

  function renderInline(nodes) {
    const h = O.h;
    return nodes.map((n) => {
      switch (n.t) {
        case "text": return document.createTextNode(vis(n.v));
        case "code": return h("code", { class: "md-code", text: vis(n.v) });
        case "strong": return h("strong", null, renderInline(n.c));
        case "em": return h("em", null, renderInline(n.c));
        case "del": return h("del", null, renderInline(n.c));
        case "br": return h("br");
        case "a":
          return n.href ? renderLink(n) : h("span", { class: "md-nolink" }, renderInline(n.c));
        case "img":
          return n.href
            ? h("a", { href: n.href, target: "_blank", rel: "noopener noreferrer", class: "md-img", title: hostOf(n.href) }, "🖼 " + vis(n.alt || "imagen"))
            : document.createTextNode(vis(n.alt || ""));
        default: return null;
      }
    });
  }

  function renderBlocks(nodes, tight) {
    const h = O.h;
    return nodes.map((n) => {
      switch (n.t) {
        case "h": return h("h" + Math.min(6, n.level + 1), { class: "md-h md-h" + n.level }, renderInline(n.c));
        case "p": return tight ? h("span", { class: "md-tight" }, renderInline(n.c)) : h("p", { class: n.rest ? "pre-wrap" : null }, renderInline(n.c));
        case "hr": return h("hr");
        case "code": {
          const pre = h("pre", { class: "md-pre" }, h("code", { class: n.lang ? "lang-" + n.lang : null, text: vis(n.text) }));
          if (O.mdCodeDecorator) O.mdCodeDecorator(pre, n);
          return pre;
        }
        case "quote": return h("blockquote", null, renderBlocks(n.c, false));
        case "ul":
        case "ol": {
          const hasTasks = n.items.some((it) => it.task !== null);
          return h(n.t, { start: n.t === "ol" && n.start !== 1 ? n.start : null, class: hasTasks ? "md-tasks" : null },
            n.items.map((it) => {
              const simple = !n.loose && it.c.length >= 1 && it.c[0].t === "p";
              const kids = [];
              if (it.task !== null) {
                kids.push(h("input", { type: "checkbox", disabled: true, checked: it.task, "aria-label": it.task ? "hecho" : "pendiente" }), " ");
              }
              if (simple) {
                kids.push(renderInline(it.c[0].c));
                kids.push(renderBlocks(it.c.slice(1), false));
              } else {
                kids.push(renderBlocks(it.c, false));
              }
              return h("li", { class: it.task !== null ? "md-task" : null }, kids);
            }));
        }
        case "table": {
          const cls = (a) => (a ? "md-al-" + a : null);
          return h("div", { class: "md-table" }, h("table", null,
            h("thead", null, h("tr", null, n.head.map((c, k) => h("th", { class: cls(n.align[k]) }, renderInline(c))))),
            h("tbody", null, n.rows.map((r) => h("tr", null, r.map((c, k) => h("td", { class: cls(n.align[k]) }, renderInline(c))))))));
        }
        default: return null;
      }
    });
  }

  /** A long input as plain text, with a button to render it anyway. */
  function renderBig(box, text) {
    const h = O.h;
    return [
      h("p", { class: "md-big-note muted small" },
        "Texto largo (" + O.fmt.bytes(O.u.bytes(text)) + "): se muestra sin formato. ",
        h("button", {
          type: "button", class: "link-btn md-big-btn",
          on: { click: () => O.put(box, renderBlocks(parse(text, { full: true }), false)) },
        }, "Renderizar igualmente")),
      h("pre", { class: "pre mono md-raw", text: vis(text) }),
    ];
  }

  /**
   * render(markdown, cls, {full}) returns a div.md with the rendered
   * content (inputs over 64 KiB as plain text unless full is set).
   */
  function render(text, cls, opts) {
    const box = O.h("div", { class: ["md", cls] });
    const nodes = parse(text, opts);
    if (nodes.length === 1 && nodes[0].t === "big") O.put(box, renderBig(box, nodes[0].text));
    else O.put(box, renderBlocks(nodes, false));
    return box;
  }

  O.md = {
    parse,
    parseInline(s) {
      nodesLeft = MAX_NODES;
      return parseInline(s, 0);
    },
    render, safeHref, MAX_AUTO_BYTES, MAX_NODES,
  };
})();
