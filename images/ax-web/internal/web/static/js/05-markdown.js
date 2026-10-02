/*
 * Oficina de agentes · safe Markdown.
 *
 * parse(text) turns Markdown into a small tree of plain objects; render()
 * builds DOM from it with createElement and text nodes only, so nothing in
 * the input is ever interpreted as markup (escape by construction). Raw
 * HTML stays visible as text. Links keep only http(s) URLs and always open
 * in a new tab with rel="noopener noreferrer". Images become links.
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
  const PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";

  const RE_FENCE = /^( {0,3})(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$/;
  const RE_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
  const RE_HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const RE_QUOTE = /^ {0,3}>[ ]?(.*)$/;
  const RE_LIST = /^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)(.*)$/;
  const RE_TABLE_DELIM = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

  const isBlank = (l) => /^[ \t]*$/.test(l);

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

  // A line that starts a block other than a paragraph (paragraphs end there).
  function startsBlock(line) {
    if (RE_FENCE.test(line) || RE_HEADING.test(line) || RE_HR.test(line) || RE_QUOTE.test(line)) return true;
    const m = RE_LIST.exec(line);
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

  function parseBlocks(lines, depth) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }

      let m = RE_FENCE.exec(line);
      if (m) {
        const indent = m[1].length, fence = m[2];
        const close = new RegExp("^ {0,3}" + (fence[0] === "`" ? "`" : "~") + "{" + fence.length + ",}[ \\t]*$");
        const body = [];
        i++;
        while (i < lines.length && !close.test(lines[i])) {
          body.push(dedent(lines[i], indent));
          i++;
        }
        i++; // closing fence (or end of input)
        out.push({ t: "code", lang: sanitizeLang(m[3]), text: body.join("\n") });
        continue;
      }

      m = RE_HEADING.exec(line);
      if (m) {
        out.push({ t: "h", level: m[1].length, c: parseInline(m[2] || "", 0) });
        i++;
        continue;
      }

      if (RE_HR.test(line)) {
        out.push({ t: "hr" });
        i++;
        continue;
      }

      if (RE_QUOTE.test(line) && depth < MAX_DEPTH) {
        const body = [];
        while (i < lines.length && !isBlank(lines[i])) {
          const q = RE_QUOTE.exec(lines[i]);
          if (q) body.push(q[1]);
          else if (!startsBlock(lines[i])) body.push(lines[i]); // lazy continuation
          else break;
          i++;
        }
        out.push({ t: "quote", c: parseBlocks(body, depth + 1) });
        continue;
      }

      m = RE_LIST.exec(line);
      if (m && depth < MAX_DEPTH) {
        const r = parseList(lines, i, depth);
        out.push(r.node);
        i = r.next;
        continue;
      }

      // GFM table: header row, delimiter row, body rows.
      if (line.includes("|") && i + 1 < lines.length && RE_TABLE_DELIM.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        const head = splitRow(line);
        const delim = splitRow(lines[i + 1]);
        if (head.length === delim.length) {
          const align = delim.map((d) => {
            const l = d.startsWith(":"), r = d.endsWith(":");
            return l && r ? "c" : r ? "r" : l ? "l" : "";
          });
          const rows = [];
          i += 2;
          while (i < lines.length && !isBlank(lines[i]) && lines[i].includes("|") && !startsBlock(lines[i])) {
            const cells = splitRow(lines[i]);
            const row = [];
            for (let k = 0; k < head.length; k++) row.push(parseInline(cells[k] || "", 0));
            rows.push(row);
            i++;
          }
          out.push({ t: "table", align, head: head.map((c) => parseInline(c, 0)), rows });
          continue;
        }
      }

      // Paragraph.
      const para = [line.replace(/^[ \t]+/, "")];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) {
        // A table starting right after paragraph text.
        if (lines[i].includes("|") && i + 1 < lines.length && RE_TABLE_DELIM.test(lines[i + 1]) && lines[i + 1].includes("-")) break;
        para.push(lines[i].replace(/^[ \t]+/, ""));
        i++;
      }
      out.push({ t: "p", c: parseInline(para.join("\n").replace(/[ \t]+$/, ""), 0) });
    }
    return out;
  }

  function parseList(lines, start, depth) {
    const first = RE_LIST.exec(lines[start]);
    const ordered = /^\d/.test(first[2]);
    const delim = first[2].slice(-1);
    const baseIndent = first[1].length;
    const node = { t: ordered ? "ol" : "ul", start: ordered ? parseInt(first[2], 10) : 1, loose: false, items: [] };
    let i = start;
    while (i < lines.length) {
      const m = RE_LIST.exec(lines[i]);
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
        if (!RE_LIST.test(l) && !startsBlock(l) && !isBlank(body[body.length - 1])) {
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
      node.items.push({ task, c: parseBlocks(body, depth + 1) });
      // Blank lines between items make the list loose.
      if (i < lines.length && isBlank(lines[i])) {
        let j = i;
        while (j < lines.length && isBlank(lines[j])) j++;
        const n = j < lines.length ? RE_LIST.exec(lines[j]) : null;
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

  /** Returns the index of the next run of exactly n backticks, or -1. */
  function findTicks(s, from, n) {
    let i = from;
    while (i < s.length) {
      const k = s.indexOf("`", i);
      if (k < 0) return -1;
      let run = 1;
      while (s[k + run] === "`") run++;
      if (run === n) return k;
      i = k + run;
    }
    return -1;
  }

  /** Destination of a link: "(url "title")" starting at s[i] === "(". */
  function linkDest(s, i) {
    let j = i + 1;
    while (s[j] === " " || s[j] === "\n") j++;
    let url = "";
    if (s[j] === "<") {
      const k = s.indexOf(">", j);
      if (k < 0 || s.slice(j, k).includes("\n")) return null;
      url = s.slice(j + 1, k);
      j = k + 1;
    } else {
      let depth = 0;
      const st = j;
      while (j < s.length) {
        const ch = s[j];
        if (ch === "\\" && j + 1 < s.length) { j += 2; continue; }
        if (ch === "(") depth++;
        else if (ch === ")") { if (depth === 0) break; depth--; }
        else if (ch === " " || ch === "\n" || ch < " ") break;
        j++;
      }
      url = s.slice(st, j).replace(/\\([!-/:-@[-`{-~])/g, "$1");
    }
    while (s[j] === " " || s[j] === "\n") j++;
    if (s[j] === "\"" || s[j] === "'") {
      const q = s[j];
      const k = s.indexOf(q, j + 1);
      if (k < 0) return null;
      j = k + 1;
      while (s[j] === " " || s[j] === "\n") j++;
    }
    if (s[j] !== ")") return null;
    return { url, end: j + 1 };
  }

  /** [text](dest) starting at s[i] === "[". */
  function parseLink(s, i) {
    let depth = 0, j = i;
    const limit = Math.min(s.length, i + 2000);
    for (; j < limit; j++) {
      const ch = s[j];
      if (ch === "\\") { j++; continue; }
      if (ch === "`") {
        let run = 1;
        while (s[j + run] === "`") run++;
        const k = findTicks(s, j + run, run);
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
    if (j >= limit || s[j] !== "]" || s[j + 1] !== "(") return null;
    const d = linkDest(s, j + 1);
    if (!d) return null;
    return { text: s.slice(i + 1, j), url: d.url, end: d.end };
  }

  /** Keeps http(s) URLs only; returns the normalized URL or null. */
  function safeHref(raw) {
    const s = String(raw || "").trim();
    if (!/^https?:\/\//i.test(s)) return null;
    if (/[\u0000- \u007f]/.test(s)) return null;
    try {
      const u = new URL(s);
      if (u.protocol !== "http:" && u.protocol !== "https:") return null;
      if (!u.hostname) return null;
      return u.href;
    } catch (e) {
      return null;
    }
  }

  // Trims trailing punctuation from a bare URL (keeping balanced parens).
  function trimUrl(u) {
    let s = u;
    for (;;) {
      const last = s[s.length - 1];
      if (/[.,:;!?'"*_~]/.test(last)) { s = s.slice(0, -1); continue; }
      if (last === ")") {
        const open = (s.match(/\(/g) || []).length, close = (s.match(/\)/g) || []).length;
        if (close > open) { s = s.slice(0, -1); continue; }
      }
      break;
    }
    return s;
  }

  const RE_URL = /https?:\/\/[^\s<>]+/iy;
  const RE_AUTOLINK = /<(https?:\/\/[^\s<>]+)>/iy;
  const isWS = (ch) => ch === undefined || /\s/.test(ch);
  const isAlnum = (ch) => ch !== undefined && /[\p{L}\p{N}]/u.test(ch);

  /**
   * Finds the closing delimiter run for an emphasis opened with len
   * characters ch, from position from. Returns the index where the closing
   * len characters start, or -1. Code spans and escapes are skipped.
   */
  function findCloser(s, from, ch, len, memo) {
    const key = ch + len;
    if (memo[key] !== undefined && from >= memo[key]) return -1;
    let i = from;
    while (i < s.length) {
      const c = s[i];
      if (c === "\\") { i += 2; continue; }
      if (c === "`") {
        let run = 1;
        while (s[i + run] === "`") run++;
        const k = findTicks(s, i + run, run);
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
    const out = [];
    const memo = {};
    let buf = "";
    const flush = () => {
      if (buf) out.push({ t: "text", v: buf });
      buf = "";
    };
    const emphasis = depth < MAX_INLINE_DEPTH && s.length <= MAX_EMPHASIS_LEN;
    let i = 0;
    while (i < s.length) {
      const c = s[i];
      if (c === "\\" && i + 1 < s.length) {
        const d = s[i + 1];
        if (d === "\n") { flush(); out.push({ t: "br" }); i += 2; continue; }
        if (PUNCT.includes(d)) { buf += d; i += 2; continue; }
        buf += c;
        i++;
        continue;
      }
      if (c === "`") {
        let run = 1;
        while (s[i + run] === "`") run++;
        const k = findTicks(s, i + run, run);
        if (k >= 0) {
          flush();
          let code = s.slice(i + run, k).replace(/\n/g, " ");
          if (code.length >= 2 && code[0] === " " && code[code.length - 1] === " " && code.trim() !== "") code = code.slice(1, -1);
          out.push({ t: "code", v: code });
          i = k + run;
          continue;
        }
        buf += "`".repeat(run);
        i += run;
        continue;
      }
      if (c === "\n") {
        if (/ {2,}$/.test(buf)) {
          buf = buf.replace(/ +$/, "");
          flush();
          out.push({ t: "br" });
        } else {
          buf = buf.replace(/ +$/, "") + "\n";
        }
        i++;
        while (s[i] === " " || s[i] === "\t") i++;
        continue;
      }
      if (depth < MAX_INLINE_DEPTH && !noLinks) {
        if (c === "!" && s[i + 1] === "[") {
          const l = parseLink(s, i + 1);
          if (l) {
            flush();
            out.push({ t: "img", alt: l.text, href: safeHref(l.url) });
            i = l.end;
            continue;
          }
        }
        if (c === "[") {
          const l = parseLink(s, i);
          if (l) {
            flush();
            out.push({ t: "a", href: safeHref(l.url), c: parseInline(l.text, depth + 1, true) });
            i = l.end;
            continue;
          }
        }
        if (c === "<") {
          RE_AUTOLINK.lastIndex = i;
          const m = RE_AUTOLINK.exec(s);
          if (m) {
            flush();
            out.push({ t: "a", href: safeHref(m[1]), c: [{ t: "text", v: m[1] }] });
            i += m[0].length;
            continue;
          }
        }
        if ((c === "h" || c === "H") && !isAlnum(s[i - 1])) {
          RE_URL.lastIndex = i;
          const m = RE_URL.exec(s);
          if (m) {
            const url = trimUrl(m[0]);
            if (url.length > 8) {
              flush();
              out.push({ t: "a", href: safeHref(url), c: [{ t: "text", v: url }] });
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
            flush();
            out.push({ t: "del", c: parseInline(s.slice(i + 2, k), depth + 1, noLinks) });
            i = k + 2;
            continue;
          }
        } else if (okOpen && c !== "~") {
          const len = run >= 2 ? 2 : 1;
          const k = findCloser(s, i + len, c, len, memo);
          if (k > i + len) {
            flush();
            // A longer opening run keeps its extra characters inside.
            const inner = s.slice(i + len, k);
            out.push({ t: len === 2 ? "strong" : "em", c: parseInline(inner, depth + 1, noLinks) });
            i = k + len;
            continue;
          }
        }
        buf += c.repeat(run);
        i += run;
        continue;
      }
      buf += c;
      i++;
    }
    flush();
    return out;
  }

  function parse(text) {
    const src = String(text || "").replace(/\r\n?/g, "\n").replace(/\u0000/g, "�");
    return parseBlocks(src.split("\n"), 0);
  }

  // ------------------------------------------------------------- render --

  function renderInline(nodes) {
    const h = O.h;
    return nodes.map((n) => {
      switch (n.t) {
        case "text": return document.createTextNode(n.v);
        case "code": return h("code", { class: "md-code", text: n.v });
        case "strong": return h("strong", null, renderInline(n.c));
        case "em": return h("em", null, renderInline(n.c));
        case "del": return h("del", null, renderInline(n.c));
        case "br": return h("br");
        case "a":
          return n.href
            ? h("a", { href: n.href, target: "_blank", rel: "noopener noreferrer" }, renderInline(n.c))
            : h("span", { class: "md-nolink" }, renderInline(n.c));
        case "img":
          return n.href
            ? h("a", { href: n.href, target: "_blank", rel: "noopener noreferrer", class: "md-img" }, "🖼 " + (n.alt || "imagen"))
            : document.createTextNode(n.alt || "");
        default: return null;
      }
    });
  }

  function renderBlocks(nodes, tight) {
    const h = O.h;
    return nodes.map((n) => {
      switch (n.t) {
        case "h": return h("h" + Math.min(6, n.level + 1), { class: "md-h md-h" + n.level }, renderInline(n.c));
        case "p": return tight ? h("span", { class: "md-tight" }, renderInline(n.c)) : h("p", null, renderInline(n.c));
        case "hr": return h("hr");
        case "code": {
          const pre = h("pre", { class: "md-pre" }, h("code", { class: n.lang ? "lang-" + n.lang : null, text: n.text }));
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

  /** render(markdown) returns a div.md with the rendered content. */
  function render(text, cls) {
    return O.h("div", { class: ["md", cls] }, renderBlocks(parse(text), false));
  }

  O.md = { parse, parseInline: (s) => parseInline(s, 0), render, safeHref };
})();
