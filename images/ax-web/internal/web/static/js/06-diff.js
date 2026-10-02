/*
 * Oficina de agentes · diffs.
 *
 * parse(patch) reads a git unified patch (`git diff --binary --full-index
 * -M`) into files, hunks and numbered lines; render() draws the viewer.
 * lines(a, b) is a small LCS line diff used to compare system prompts.
 *
 * Patches come from agents (hostile, up to several MiB): parsing is linear,
 * with string scans instead of regexes over whole lines, and the viewer
 * draws invisible characters as ⟦U+XXXX⟧ markers (O.visible).
 */
(() => {
  "use strict";
  const O = window.Oficina;

  const ENC = new TextEncoder(), DEC = new TextDecoder();
  const ESCAPES = { n: "\n", t: "\t", r: "\r", a: "\x07", b: "\b", f: "\f", v: "\v", "\\": "\\", "\"": "\"" };

  // Git C-style quoted path: "a/caf\303\251 \"x\"" -> a/café "x".
  // Runs of plain characters are encoded at once.
  function unquote(s) {
    if (!s || s[0] !== "\"") return s;
    const bytes = [];
    const pushStr = (str) => {
      if (str) for (const b of ENC.encode(str)) bytes.push(b);
    };
    let from = 1, i = 1;
    for (; i < s.length; i++) {
      const ch = s[i];
      if (ch !== "\"" && ch !== "\\") continue;
      pushStr(s.slice(from, i));
      if (ch === "\"") {
        from = -1;
        break;
      }
      const n = s[++i];
      if (n >= "0" && n <= "7") {
        bytes.push(parseInt(s.slice(i, i + 3), 8) & 255);
        i += 2;
      } else {
        pushStr(O.own(ESCAPES, n) !== undefined ? ESCAPES[n] : n || "");
      }
      from = i + 1;
    }
    if (from >= 0 && from < s.length) pushStr(s.slice(from)); // no closing quote
    return DEC.decode(new Uint8Array(bytes));
  }

  // Takes the first (possibly quoted) token of s; returns [token, rest].
  function token(s) {
    if (s[0] === "\"") {
      let i = 1;
      for (; i < s.length; i++) {
        if (s[i] === "\\") { i++; continue; }
        if (s[i] === "\"") break;
      }
      return [unquote(s.slice(0, i + 1)), s.slice(i + 1).replace(/^ /, "")];
    }
    return [s, ""];
  }

  function stripPrefix(p, prefix) {
    if (p === null || p === undefined) return null;
    if (p === "/dev/null") return null;
    return p.startsWith(prefix) ? p.slice(prefix.length) : p;
  }

  // "diff --git a/X b/Y" -> [X, Y] (best effort when unquoted with spaces).
  function headerPaths(rest) {
    if (rest[0] === "\"") {
      const [a, r] = token(rest);
      const [b] = r[0] === "\"" ? token(r) : [r];
      return [stripPrefix(a, "a/"), stripPrefix(b, "b/")];
    }
    const quotedB = rest.indexOf(" \"b/");
    if (quotedB > 0) {
      return [stripPrefix(rest.slice(0, quotedB), "a/"), stripPrefix(token(rest.slice(quotedB + 1))[0], "b/")];
    }
    // Same path on both sides: "a/" + p + " b/" + p.
    if (rest.startsWith("a/") && (rest.length - 5) % 2 === 0) {
      const len = (rest.length - 5) / 2;
      const a = rest.slice(2, 2 + len), b = rest.slice(2 + len + 3);
      if (a === b && rest.slice(2 + len, 2 + len + 3) === " b/") return [a, b];
    }
    const k = rest.lastIndexOf(" b/");
    if (k > 0) return [stripPrefix(rest.slice(0, k), "a/"), rest.slice(k + 3)];
    return [rest, rest];
  }

  // "--- a/path" header value; git appends a TAB when the name has spaces.
  function headerPath(v, prefix) {
    let s = v;
    if (s[0] === "\"") s = token(s)[0];
    else {
      const i = s.indexOf("\t");
      if (i >= 0) s = s.slice(0, i);
    }
    return stripPrefix(s, prefix);
  }

  // Only the fixed head of the line: the section after "@@" is sliced off
  // (a regex like " ?(.*)$" backtracks on lines with U+2028 or a lone CR).
  const RE_HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

  function parse(text) {
    const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    const files = [];
    let f = null, hunk = null, oldLeft = 0, newLeft = 0, oldNo = 0, newNo = 0, inBinary = false;

    const newFile = (oldPath, newPath) => {
      f = { oldPath, newPath, path: newPath || oldPath || "", status: "M", binary: false, similarity: null,
        oldMode: "", newMode: "", hunks: [], additions: 0, deletions: 0, hasOld: true, hasNew: true };
      files.push(f);
      hunk = null;
      oldLeft = newLeft = 0;
      inBinary = false;
    };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      // Body of a hunk while its counts last.
      if (hunk && (oldLeft > 0 || newLeft > 0)) {
        const c = line[0];
        if (c === " " || line === "") {
          hunk.lines.push({ k: "ctx", t: line.slice(1), o: oldNo++, n: newNo++ });
          oldLeft--; newLeft--;
          continue;
        }
        if (c === "-") {
          hunk.lines.push({ k: "del", t: line.slice(1), o: oldNo++, n: null });
          f.deletions++;
          oldLeft--;
          continue;
        }
        if (c === "+") {
          hunk.lines.push({ k: "add", t: line.slice(1), o: null, n: newNo++ });
          f.additions++;
          newLeft--;
          continue;
        }
        if (c === "\\") {
          const last = hunk.lines[hunk.lines.length - 1];
          if (last) last.nonl = true;
          continue;
        }
        // Malformed: fall through to header handling.
        oldLeft = newLeft = 0;
      }
      if (line.startsWith("diff --git ")) {
        const [a, b] = headerPaths(line.slice(11));
        newFile(a, b);
        continue;
      }
      if (!f) {
        // Plain unified diff without a git header.
        if (line.startsWith("--- ") && i + 1 < lines.length && lines[i + 1].startsWith("+++ ")) newFile(null, null);
        else continue;
      }
      if (inBinary) continue;
      if (line.startsWith("\\")) {
        const last = hunk && hunk.lines[hunk.lines.length - 1];
        if (last) last.nonl = true;
        continue;
      }
      const hm = RE_HUNK.exec(line);
      if (hm) {
        oldNo = parseInt(hm[1], 10);
        newNo = parseInt(hm[3], 10);
        oldLeft = hm[2] === undefined ? 1 : parseInt(hm[2], 10);
        newLeft = hm[4] === undefined ? 1 : parseInt(hm[4], 10);
        let section = line.slice(hm[0].length);
        if (section[0] === " ") section = section.slice(1);
        hunk = { header: line, oldStart: oldNo, newStart: newNo, oldLines: oldLeft, newLines: newLeft, section, lines: [] };
        f.hunks.push(hunk);
        continue;
      }
      if (line.startsWith("--- ")) {
        const p = headerPath(line.slice(4), "a/");
        if (p === null) { f.hasOld = false; f.status = "A"; } else f.oldPath = p;
        continue;
      }
      if (line.startsWith("+++ ")) {
        const p = headerPath(line.slice(4), "b/");
        if (p === null) { f.hasNew = false; f.status = "D"; } else f.newPath = p;
        continue;
      }
      if (line.startsWith("new file mode ")) { f.status = "A"; f.hasOld = false; f.newMode = line.slice(14); continue; }
      if (line.startsWith("deleted file mode ")) { f.status = "D"; f.hasNew = false; f.oldMode = line.slice(18); continue; }
      if (line.startsWith("old mode ")) { f.oldMode = line.slice(9); continue; }
      if (line.startsWith("new mode ")) { f.newMode = line.slice(9); if (f.status === "M") f.status = "T"; continue; }
      if (line.startsWith("rename from ")) { f.oldPath = unquote(line.slice(12)); f.status = "R"; continue; }
      if (line.startsWith("rename to ")) { f.newPath = unquote(line.slice(10)); f.status = "R"; continue; }
      if (line.startsWith("copy from ")) { f.oldPath = unquote(line.slice(10)); f.status = "C"; continue; }
      if (line.startsWith("copy to ")) { f.newPath = unquote(line.slice(8)); f.status = "C"; continue; }
      if (line.startsWith("similarity index ")) { f.similarity = parseInt(line.slice(17), 10); continue; }
      if (line.startsWith("Binary files ") && line.endsWith(" differ")) { f.binary = true; continue; }
      if (line === "GIT binary patch") { f.binary = true; inBinary = true; continue; }
    }
    for (const x of files) {
      if (x.status === "D") x.path = x.oldPath || x.newPath || "";
      else x.path = x.newPath || x.oldPath || "";
      if (x.status === "T" && x.hunks.length) x.status = "M";
    }
    return files;
  }

  // ------------------------------------------------------- line diff (LCS) --

  /** lines(a, b) returns [{k: "ctx"|"del"|"add", t}] turning a into b. */
  function lines(a, b) {
    const A = String(a || "").split("\n"), B = String(b || "").split("\n");
    // Common prefix and suffix keep the table small.
    let pre = 0;
    while (pre < A.length && pre < B.length && A[pre] === B[pre]) pre++;
    let suf = 0;
    while (suf < A.length - pre && suf < B.length - pre && A[A.length - 1 - suf] === B[B.length - 1 - suf]) suf++;
    const a2 = A.slice(pre, A.length - suf), b2 = B.slice(pre, B.length - suf);
    const out = [];
    for (let i = 0; i < pre; i++) out.push({ k: "ctx", t: A[i] });
    const n = a2.length, m = b2.length, W = m + 1;
    if ((n + 1) * W > 4e6) {
      for (const t of a2) out.push({ k: "del", t });
      for (const t of b2) out.push({ k: "add", t });
    } else {
      // dp[i * W + j] = LCS length of a2[i:] and b2[j:] (one flat table).
      const dp = new Uint32Array((n + 1) * W);
      for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
          dp[i * W + j] = a2[i] === b2[j] ? dp[(i + 1) * W + j + 1] + 1 : Math.max(dp[(i + 1) * W + j], dp[i * W + j + 1]);
        }
      }
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (a2[i] === b2[j]) { out.push({ k: "ctx", t: a2[i] }); i++; j++; }
        else if (dp[(i + 1) * W + j] >= dp[i * W + j + 1]) { out.push({ k: "del", t: a2[i] }); i++; }
        else { out.push({ k: "add", t: b2[j] }); j++; }
      }
      for (; i < n; i++) out.push({ k: "del", t: a2[i] });
      for (; j < m; j++) out.push({ k: "add", t: b2[j] });
    }
    for (let i = A.length - suf; i < A.length; i++) out.push({ k: "ctx", t: A[i] });
    return out;
  }

  // ------------------------------------------------------------- render --

  const STATUS = {
    A: ["A", "añadido"], M: ["M", "modificado"], D: ["D", "borrado"], R: ["R", "renombrado"],
    C: ["C", "copiado"], T: ["T", "cambio de tipo"],
  };

  function statBar(add, del) {
    const total = add + del;
    const h = O.h;
    const bar = h("span", { class: "diff-bar", "aria-hidden": "true" });
    const blocks = 5;
    const greens = total ? Math.round((add / total) * blocks) : 0;
    const reds = total ? Math.min(blocks - greens, Math.round((del / total) * blocks)) : 0;
    for (let i = 0; i < blocks; i++) {
      bar.appendChild(h("i", { class: i < greens ? "is-add" : i < greens + reds ? "is-del" : "" }));
    }
    return bar;
  }

  function fileRows(file) {
    const h = O.h;
    const rows = [];
    for (const hk of file.hunks) {
      rows.push(h("tr", { class: "diff-hunk" },
        h("td", { class: "diff-ln", colspan: "2", "aria-hidden": "true", text: "⋯" }),
        h("td", { class: "diff-code", text: O.visible(hk.header) })));
      for (const l of hk.lines) {
        rows.push(h("tr", { class: "diff-" + l.k },
          h("td", { class: "diff-ln", text: l.o === null ? "" : String(l.o) }),
          h("td", { class: "diff-ln", text: l.n === null ? "" : String(l.n) }),
          h("td", { class: "diff-code" },
            h("span", { class: "diff-sign", "aria-hidden": "true", text: l.k === "add" ? "+" : l.k === "del" ? "-" : " " }),
            h("span", { class: "sr-only", text: l.k === "add" ? "añadida: " : l.k === "del" ? "borrada: " : "" }),
            O.visible(l.t),
            l.nonl ? h("span", { class: "diff-nonl", title: "Sin salto de línea al final", text: " ⏎̸" }) : null)));
      }
    }
    return rows;
  }

  /**
   * render(files, {fileStats}) draws a file list with ± stats and a
   * collapsible unified diff per file. fileStats (harness.ChangedFile[])
   * completes the numbers when the patch was truncated.
   */
  function render(files, opts) {
    const h = O.h;
    const o = opts || {};
    const totalAdd = files.reduce((n, f) => n + f.additions, 0);
    const totalDel = files.reduce((n, f) => n + f.deletions, 0);
    const sections = [];
    const list = h("ul", { class: "diff-files" });
    const bigTotal = files.reduce((n, f) => n + f.hunks.reduce((m, hk) => m + hk.lines.length, 0), 0) > 3000;

    files.forEach((f) => {
      const st = O.own(STATUS, f.status) || STATUS.M;
      const label = O.visible(f.status === "R" || f.status === "C" ? (f.oldPath || "") + " → " + (f.newPath || "") : f.path);
      const lineCount = f.hunks.reduce((m, hk) => m + hk.lines.length, 0);
      const body = h("div", { class: "diff-body" });
      const details = h("details", { class: "diff-file", open: !bigTotal && lineCount <= 600 ? true : null },
        h("summary", { class: "diff-file-head" },
          h("span", { class: "diff-status diff-st-" + f.status, title: st[1], text: st[0] }),
          h("span", { class: "diff-path mono", text: label }),
          f.binary ? h("span", { class: "chip", text: "binario" }) : null,
          f.similarity !== null && f.status === "R" ? h("span", { class: "chip", text: f.similarity + " %" }) : null,
          h("span", { class: "diff-nums" },
            h("span", { class: "diff-plus", text: "+" + f.additions }),
            h("span", { class: "diff-minus", text: "−" + f.deletions }))),
        body);
      let drawn = false;
      const draw = () => {
        if (drawn) return;
        drawn = true;
        if (f.binary) {
          body.appendChild(h("p", { class: "muted pad", text: "Fichero binario: no se muestra el contenido." }));
        } else if (!f.hunks.length) {
          body.appendChild(h("p", { class: "muted pad", text: f.status === "R" ? "Renombrado sin cambios de contenido." : "Sin cambios de contenido (permisos o fichero vacío)." }));
        } else {
          body.appendChild(h("div", { class: "diff-scroll" },
            h("table", { class: "diff-table" }, h("tbody", null, fileRows(f)))));
        }
      };
      if (details.open) draw();
      details.addEventListener("toggle", () => { if (details.open) draw(); });
      sections.push(details);
      list.appendChild(h("li", null,
        h("button", {
          type: "button", class: "diff-jump",
          on: { click: () => { details.open = true; draw(); details.scrollIntoView({ behavior: "smooth", block: "start" }); } },
        },
        h("span", { class: "diff-status diff-st-" + f.status, title: st[1], text: st[0] }),
        h("span", { class: "diff-path mono", text: label }),
        h("span", { class: "diff-nums" },
          h("span", { class: "diff-plus", text: "+" + f.additions }),
          h("span", { class: "diff-minus", text: "−" + f.deletions })),
        statBar(f.additions, f.deletions))));
    });

    return h("div", { class: "diff" },
      h("div", { class: "diff-summary" },
        h("strong", { text: files.length + " " + O.u.plural(files.length, "fichero", "ficheros") }),
        h("span", { class: "diff-plus", text: "+" + totalAdd }),
        h("span", { class: "diff-minus", text: "−" + totalDel }),
        o.extra || null),
      list, sections);
  }

  /** renderLines(ops) draws a lines() result as a compact unified view. */
  function renderLines(ops, context) {
    const h = O.h;
    const ctx = context === undefined ? 3 : context;
    const keep = new Array(ops.length).fill(false);
    ops.forEach((op, i) => {
      if (op.k === "ctx") return;
      for (let k = Math.max(0, i - ctx); k <= Math.min(ops.length - 1, i + ctx); k++) keep[k] = true;
    });
    const rows = [];
    let skipped = 0;
    ops.forEach((op, i) => {
      if (!keep[i]) { skipped++; return; }
      if (skipped) {
        rows.push(h("div", { class: "ldiff-skip", text: "⋯ " + skipped + " " + O.u.plural(skipped, "línea igual", "líneas iguales") }));
        skipped = 0;
      }
      rows.push(h("div", { class: "ldiff-" + op.k },
        h("span", { class: "diff-sign", "aria-hidden": "true", text: op.k === "add" ? "+" : op.k === "del" ? "-" : " " }),
        h("span", { class: "sr-only", text: op.k === "add" ? "añadida: " : op.k === "del" ? "borrada: " : "" }),
        op.t ? O.visible(op.t) : " "));
    });
    if (skipped) rows.push(h("div", { class: "ldiff-skip", text: "⋯ " + skipped + " " + O.u.plural(skipped, "línea igual", "líneas iguales") }));
    if (!ops.some((op) => op.k !== "ctx")) rows.unshift(h("div", { class: "ldiff-skip", text: "Sin diferencias en el texto." }));
    return h("div", { class: "ldiff mono" }, rows);
  }

  O.diff = { parse, lines, render, renderLines, unquote };
})();
