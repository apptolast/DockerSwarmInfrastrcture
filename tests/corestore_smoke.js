"use strict";
// Smoke test of the API client and the SSE reconnection logic without a
// browser: js/00-core.js and js/10-store.js run in a vm with a fake
// EventSource, a fake fetch and recorded timers (no real waits), so a wrong
// backoff, a stale-source race or a dropped error message fails in CI
// instead of in the owner's browser. Run by tests/test_ax_web_corestore.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const root = path.resolve(__dirname, "../images/ax-web/internal/web/static/js");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");
const CORE_SRC = read("00-core.js");
const STORE_SRC = read("10-store.js");

// ------------------------------------------------------------- fake timers --

const timers = new Map();
let timerId = 0;
function fakeSetTimeout(fn, ms) {
  const id = ++timerId;
  timers.set(id, { fn, ms });
  return id;
}
function fakeClearTimeout(id) {
  timers.delete(id);
}
function fireTimeout(id) {
  const t = timers.get(id);
  if (!t) return;
  timers.delete(id);
  t.fn();
}
/** Fires the one new timer scheduled while running fn, returns its ms. */
function fireNewTimer(fn) {
  const before = new Set(timers.keys());
  fn();
  const added = Array.from(timers.keys()).filter((id) => !before.has(id));
  assert.strictEqual(added.length, 1, "expected exactly one new timer, got " + added.length);
  const ms = timers.get(added[0]).ms;
  fireTimeout(added[0]);
  return ms;
}

// ---------------------------------------------------------- fake EventSource --

const esInstances = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.listeners = {};
    this.onerror = null;
    this.closed = false;
    esInstances.push(this);
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  close() {
    this.readyState = 2;
    this.closed = true;
  }
  fire(type, data) {
    for (const f of this.listeners[type] || []) f({ data: JSON.stringify(data) });
  }
}

// ------------------------------------------------------------ fake document --

function makeTarget() {
  const listeners = {};
  return {
    listeners,
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
    },
  };
}
function emit(target, type, event) {
  for (const f of target.listeners[type] || []) f(event);
}

// --------------------------------------------------------------- the vm --

async function flush(n) {
  for (let i = 0; i < (n || 20); i++) await Promise.resolve();
}

function buildContext() {
  const doc = Object.assign({ visibilityState: "visible" }, makeTarget());
  const ctx = vm.createContext({ console, EventSource: FakeEventSource, document: doc });
  Object.assign(ctx, makeTarget());
  ctx.window = ctx;
  ctx.setTimeout = fakeSetTimeout;
  ctx.clearTimeout = fakeClearTimeout;
  vm.runInContext(CORE_SRC, ctx, { filename: "00-core.js" });
  vm.runInContext(STORE_SRC, ctx, { filename: "10-store.js" });
  return { ctx, doc };
}

// ----------------------------------------------------------- 00-core.js --

function testSafeLink(O) {
  assert.strictEqual(O.safeLink("/a\tb"), null, "a tab in a path must be rejected");
  assert.strictEqual(O.safeLink("/\\evil.com"), null, "a backslash must be rejected");
  assert.strictEqual(O.safeLink(""), null, "an empty value must be rejected");
  assert.strictEqual(O.safeLink("   "), null, "a blank value must be rejected");
  assert.strictEqual(O.safeLink("#frag"), "#frag", "a fragment must be accepted");
  assert.strictEqual(O.safeLink("/ruta"), "/ruta", "a same-origin path must be accepted");
  assert.strictEqual(O.safeLink("//evil.com"), null, "a protocol-relative URL must be rejected");
  assert.strictEqual(O.safeLink("https://example.com/x"), "https://example.com/x", "an https URL must be accepted");
  assert.strictEqual(O.safeLink("javascript:alert(1)"), null, "a javascript: URL must be rejected");
}

function testInvisible(O) {
  assert.strictEqual(O.visible("a\u0007b"), "a⟦U+0007⟧b", "a control character must be marked");
  // Array.from() re-homes the vm-realm array so deepStrictEqual does not
  // trip over its foreign prototype.
  assert.deepStrictEqual(Array.from(O.invisibles("a\u0007b")), ["⟦U+0007⟧"]);
  const techWoman = "\u{1F469}\u200D\u{1F4BB}"; // woman + ZWJ + laptop
  assert.strictEqual(O.visible(techWoman), techWoman, "a ZWJ inside an emoji sequence must not be marked");
  assert.deepStrictEqual(Array.from(O.invisibles(techWoman)), []);
}

const HTTP_MESSAGES = {
  401: "La sesión caducó: recarga la página para identificarte de nuevo.",
  403: "La Oficina rechazó la petición.",
  404: "No existe (puede que se haya borrado).",
  409: "No se puede hacer ahora mismo (conflicto).",
  413: "La petición es demasiado grande.",
  429: "Demasiadas peticiones seguidas; espera unos segundos.",
  502: "El servidor no responde ahora mismo.",
  503: "El servidor no responde ahora mismo.",
  504: "El servidor no responde ahora mismo.",
  418: "Error 418 del servidor.",
};

async function testRequest(ctx, O) {
  // A fetch that throws must become a stable, Spanish network error.
  ctx.fetch = () => Promise.reject(new Error("boom"));
  let err = await O.api.get("/y").catch((e) => e);
  assert.ok(err instanceof O.ApiError, "a network failure must raise an ApiError");
  assert.strictEqual(err.message, "No se pudo contactar con la Oficina. ¿Hay conexión?");
  assert.strictEqual(err.status, 0);

  // A JSON error body names the field and the message the server chose.
  ctx.fetch = async () => ({ ok: false, status: 409, text: async () => JSON.stringify({ error: "conflicto X", field: "nombre" }) });
  err = await O.api.get("/y").catch((e) => e);
  assert.strictEqual(err.message, "conflicto X");
  assert.strictEqual(err.field, "nombre");
  assert.strictEqual(err.status, 409);

  // Without a JSON body, every status falls back to the fixed Spanish message.
  for (const [code, msg] of Object.entries(HTTP_MESSAGES)) {
    ctx.fetch = async () => ({ ok: false, status: Number(code), text: async () => "plain text, not json" });
    err = await O.api.get("/y").catch((e) => e);
    assert.strictEqual(err.message, msg, "status " + code);
    assert.strictEqual(err.status, Number(code));
  }

  // An OK response that is not JSON must not be trusted as one.
  ctx.fetch = async () => ({ ok: true, status: 200, text: async () => "<html>not json</html>" });
  err = await O.api.get("/y").catch((e) => e);
  assert.strictEqual(err.message, "La Oficina respondió algo que no es JSON.");

  // An OK response with an empty body is treated as "{}".
  ctx.fetch = async () => ({ ok: true, status: 200, text: async () => "" });
  const empty = await O.api.get("/y");
  assert.strictEqual(Object.keys(empty).length, 0, "an empty OK body must become {}");
}

async function testSemaphore(ctx, O) {
  const calls = [];
  ctx.fetch = (path) => {
    let resolve, reject;
    const p = new Promise((re, rj) => { resolve = re; reject = rj; });
    calls.push({ path, resolve, reject });
    return p;
  };
  const results = [];
  for (let i = 0; i < 6; i++) {
    O.api.get("/x" + i).then((v) => results.push({ i, ok: true, v }), (e) => results.push({ i, ok: false, e }));
  }
  await flush();
  assert.strictEqual(calls.length, 4, "only 4 of 6 requests should be in flight at once");

  // Four of the six requests fail outright.
  for (let i = 0; i < 4; i++) calls[i].reject(new Error("net fail " + i));
  await flush();
  assert.strictEqual(results.length, 4, "the 4 failed requests must settle before the queue moves on");
  assert.ok(results.every((r) => !r.ok), "the first 4 results must all be failures");
  assert.strictEqual(calls.length, 6, "a failure must free a slot for a queued request (the finally, not a catch)");

  // The 2 that had been waiting succeed once their turn comes.
  calls[4].resolve({ ok: true, status: 200, text: async () => "{}" });
  calls[5].resolve({ ok: true, status: 200, text: async () => "{}" });
  await flush();
  assert.strictEqual(results.length, 6, "all 6 requests must settle, even after 4 failures filled the semaphore");
}

// ---------------------------------------------------------- 10-store.js --

function testApplyDeltaDiscardsStale(O) {
  const S = O.state;
  S.snap = { rev: 10, jobs: [], queue: [], active: null, ax: {} };
  let emits = 0;
  O.on("store", () => emits++);
  O.store.applyDelta({ rev: 5, jobs: [] });
  assert.strictEqual(S.snap.rev, 10, "a delta older than the snapshot must be discarded");
  assert.strictEqual(emits, 0, "a discarded delta must not notify the UI");
  O.store.applyDelta({ rev: 11, jobs: [] });
  assert.strictEqual(S.snap.rev, 11, "a newer delta must be applied");
  assert.strictEqual(emits, 1);
}

function testStaleSourceGuardAndCoalescing(ctx, O, doc) {
  const S = O.state;
  S.snap = { rev: 1, jobs: [], queue: [], active: null, ax: {} };
  esInstances.length = 0;
  for (const [id] of Array.from(timers)) timers.delete(id);

  O.stream.start();
  assert.strictEqual(esInstances.length, 1, "start() must open exactly one stream");
  const first = esInstances[0];

  // watch() in the same tick must coalesce into a single reconnect.
  O.stream.watch("route", "j1");
  O.stream.watch("drawer", "j2");
  const before = esInstances.length;
  const added = Array.from(timers.keys());
  assert.strictEqual(added.length, 1, "two watch() calls in one tick must schedule a single timer");
  fireTimeout(added[0]);
  assert.strictEqual(esInstances.length, before + 1, "coalesced watch() calls must reconnect exactly once");
  const second = esInstances[esInstances.length - 1];
  assert.notStrictEqual(first, second, "watch() must open a new stream");

  // An event on the superseded (stale) stream must not touch the state.
  first.fire("office", { rev: 5, jobs: [] });
  assert.strictEqual(S.snap.rev, 1, "a delta from a superseded EventSource must be ignored");

  // The same event on the live stream must be applied.
  second.fire("office", { rev: 5, jobs: [] });
  assert.strictEqual(S.snap.rev, 5, "a delta from the live EventSource must be applied");
}

async function testHello(ctx, O) {
  const S = O.state;
  S.snap = { rev: 3, jobs: [], queue: [], active: null, ax: {} };
  S.conn = "connecting";
  esInstances.length = 0;
  for (const [id] of Array.from(timers)) timers.delete(id);

  ctx.fetch = async () => ({ ok: true, status: 200, text: async () => JSON.stringify({ rev: 3, now: new Date().toISOString() }) });

  O.stream.start();
  const live = esInstances[esInstances.length - 1];

  const ms = fireNewTimer(() => live.fire("hello", { rev: 5 }));
  assert.strictEqual(S.conn, "live", "a hello must mark the connection as live");
  assert.ok(ms >= 1000, "a mismatched revision must schedule a snapshot reload, not fetch instantly");
  await flush();

  // Backoff must have been reset to 0 by the hello: the next failure waits the base 5s, not longer.
  live.readyState = 2;
  const backoffMs = fireNewTimer(() => live.onerror());
  assert.strictEqual(backoffMs, 5000, "backoff must restart from the base delay right after a hello");
}

function testBackoffSequence(ctx, O) {
  esInstances.length = 0;
  for (const [id] of Array.from(timers)) timers.delete(id);
  O.stream.start();
  let src = esInstances[esInstances.length - 1];

  const expected = [5000, 10000, 20000, 40000, 60000, 60000];
  for (const want of expected) {
    src.readyState = 2;
    const ms = fireNewTimer(() => src.onerror());
    assert.strictEqual(ms, want, "backoff delay");
    src = esInstances[esInstances.length - 1];
  }

  // A failure while the stream is merely connecting (not closed) must not reconnect on a timer.
  const beforeTimers = timers.size;
  src.readyState = 0;
  src.onerror();
  assert.strictEqual(timers.size, beforeTimers, "a non-fatal error must not schedule a reconnect");
}

function testVisibilityAndOnline(ctx, O, doc) {
  esInstances.length = 0;
  for (const [id] of Array.from(timers)) timers.delete(id);
  O.stream.start();

  // Drive backoff above 5s, then becoming visible again must reset it and reconnect right away.
  let src = esInstances[esInstances.length - 1];
  src.readyState = 2;
  fireNewTimer(() => src.onerror()); // backoff -> 5000, retryTimer scheduled and fired -> reconnected
  src = esInstances[esInstances.length - 1];
  src.readyState = 2;
  src.onerror(); // backoff -> 10000, retryTimer pending (not fired)
  assert.ok(timers.size >= 1, "a pending retry timer is expected");

  const before = esInstances.length;
  doc.visibilityState = "visible";
  emit(doc, "visibilitychange", {});
  assert.strictEqual(esInstances.length, before + 1, "becoming visible with backoff > 5s must reconnect immediately");

  // The "online" handler reconnects when the current stream is closed.
  const current = esInstances[esInstances.length - 1];
  current.readyState = 2;
  emit(ctx, "online", {});
  assert.strictEqual(esInstances.length, before + 2, "coming back online with a closed stream must reconnect");
}

// -------------------------------------------------------------------- main --

(async () => {
  {
    const { ctx } = buildContext();
    testSafeLink(ctx.window.Oficina);
    testInvisible(ctx.window.Oficina);
  }
  {
    const { ctx } = buildContext();
    await testRequest(ctx, ctx.window.Oficina);
  }
  {
    const { ctx } = buildContext();
    await testSemaphore(ctx, ctx.window.Oficina);
  }
  {
    const { ctx } = buildContext();
    testApplyDeltaDiscardsStale(ctx.window.Oficina);
  }
  {
    const { ctx, doc } = buildContext();
    testStaleSourceGuardAndCoalescing(ctx, ctx.window.Oficina, doc);
  }
  {
    const { ctx } = buildContext();
    await testHello(ctx, ctx.window.Oficina);
  }
  {
    const { ctx } = buildContext();
    testBackoffSequence(ctx, ctx.window.Oficina);
  }
  {
    const { ctx, doc } = buildContext();
    testVisibilityAndOnline(ctx, ctx.window.Oficina, doc);
  }
  console.log("corestore smoke: ok");
})().catch((e) => {
  const lines = String(e && e.stack ? e.stack : e).split("\n");
  console.error(lines.filter((l, i) => i < 4 || l.length < 300).map((l) => l.slice(0, 900)).join("\n"));
  process.exit(1);
});
