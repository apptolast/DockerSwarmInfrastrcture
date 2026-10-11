"use strict";
// Smoke test of the Oficina 3D view without a browser. The real trimmed
// Three.js and the real scene run in a vm with a fake DOM and a fake WebGL
// renderer, and the real view (js/51-office3d.js) runs against a stub of the
// panel, so a wrong API name, a leaked resource, a missed tap or a logic slip
// in the view fails in CI instead of in the owner's browser. Run by
// tests/test_ax_web_office3d.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const root = path.resolve(__dirname, "../images/ax-web/internal/web/static");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");
const THREE_SRC = read("3d/00-three.js");
const SCENE_SRC = read("3d/10-office3d.js");
const VIEW_SRC = read("js/51-office3d.js");

function fakeElement(tag, document) {
  const el = {
    tag, style: {}, children: [], attrs: {}, listeners: {}, clientWidth: 1000, clientHeight: 600, width: 0, height: 0,
    ownerDocument: document || null, putCount: 0, textContent: "",
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); },
    prepend(c) { this.children.unshift(c); c.parent = this; },
    appendChild(c) { this.children = this.children.filter((x) => x !== c); this.children.push(c); c.parent = this; return c; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.removed = true; },
    getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; },
    setPointerCapture() {}, releasePointerCapture() {},
    getRootNode() { return { addEventListener() {}, removeEventListener() {} }; },
    getContext() {
      return { clearRect() {}, fillText() {}, measureText: () => ({ width: 10 }), set font(v) {}, set fillStyle(v) {}, set textAlign(v) {}, set textBaseline(v) {} };
    },
  };
  return el;
}

function emit(el, type, event) {
  for (const f of el.listeners[type] || []) f(event);
}

// ------------------------------------------------------------------ scene --

function runScene({ reduceMotion, width }) {
  const clock = { t: 1000 };
  const frames = [];
  const timers = new Map();
  let timerId = 0;
  const warnings = [];
  const stats = { renderers: [], io: [], ro: [] };
  const live = new Set();
  const document = {
    hidden: false, listeners: {},
    createElement: (tag) => fakeElement(tag, document),
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f); },
  };
  const ctx = vm.createContext({
    document, navigator: { hardwareConcurrency: 8 }, devicePixelRatio: 1, performance: { now: () => (clock.t += 16) },
    console: { log() {}, warn: (...a) => warnings.push(a.join(" ")), error: (...a) => warnings.push(a.join(" ")) },
    matchMedia: () => ({ matches: reduceMotion }),
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    cancelAnimationFrame() {},
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: (id) => { timers.delete(id); },
    IntersectionObserver: class { constructor(cb) { this.cb = cb; stats.io.push(this); } observe() {} disconnect() { this.gone = true; } },
    ResizeObserver: class { constructor(cb) { this.cb = cb; stats.ro.push(this); } observe() {} disconnect() { this.gone = true; } },
  });
  ctx.window = ctx;
  vm.runInContext(THREE_SRC, ctx, { filename: "00-three.js" });
  const real = ctx.THREE;
  assert.ok(real && real.OrbitControls && real.REVISION, "Three.js did not define THREE");
  // The exports are read-only getters: the scene gets a copy. Every class that
  // owns GPU memory counts itself while alive, and the renderer has no GPU.
  const T = Object.assign({}, real);
  for (const name of Object.keys(real)) {
    const K = real[name];
    // Only what owns GPU memory: geometries, materials and textures. Scene objects (meshes, groups,
    // sprites, lights) also expose dispose() in this version but hold nothing on the GPU.
    if (typeof K === "function" && K.prototype && typeof K.prototype.dispose === "function" && name !== "OrbitControls") {
      T[name] = class extends K {
        constructor(...a) {
          super(...a);
          if (this.isObject3D) return; // decided on the instance: it is not a prototype flag in this version
          this.kind = name;
          live.add(this);
        }
        dispose() { live.delete(this); super.dispose(); }
      };
    }
  }
  T.WebGLRenderer = class {
    constructor() {
      this.domElement = fakeElement("canvas", document);
      this.shadowMap = {};
      this.frames = 0;
      stats.renderers.push(this);
    }
    setPixelRatio() {} setSize() {}
    render() { this.frames++; }
    setAnimationLoop(fn) { this.loop = fn; }
    dispose() { this.disposed = true; }
  };
  ctx.THREE = T;
  vm.runInContext(SCENE_SRC, ctx, { filename: "10-office3d.js" });
  const api = ctx.Oficina3D;
  assert.ok(api && typeof api.create === "function", "Oficina3D.create is missing");

  const host = fakeElement("div", document);
  host.clientWidth = width;
  const picks = [];
  const lost = [];
  const notices = [];
  const scene = api.create(host, {
    onPick: (id) => picks.push(id), onFallback: (m) => lost.push(m), onNotice: (m) => notices.push(m),
  });
  assert.ok(scene, "create returned null with a renderer available");
  const renderer = stats.renderers[0];
  assert.strictEqual(host.children[0], renderer.domElement, "the canvas was not put in the host");
  assert.strictEqual(renderer.shadowMap.autoUpdate, false, "shadows are redrawn on every frame");
  assert.strictEqual(renderer.shadowMap.enabled, width >= 600, "shadows must be off on a narrow screen");
  // With motion welcome a loop frame advances the pulses; with reduced motion nothing runs by itself.
  // Firing a timer by hand removes it, as a real clock does.
  const fire = (pick) => {
    for (const [id, tm] of Array.from(timers)) {
      if (!pick(tm)) continue;
      timers.delete(id);
      tm.fn();
    }
  };
  const drain = () => { if (!reduceMotion) for (let i = 0; i < 400; i++) renderer.loop && renderer.loop(); };

  const agents = (n) => Array.from({ length: n }, (_, i) => ({
    id: "a" + i, name: "Agente " + i, emoji: i % 2 ? "🛠️" : "🏛️", color: i === 3 ? "not-a-colour" : "#22c55e",
    state: ["working", "attention", "queued", "idle", "sleeping"][i % 5],
    title: "Un título de trabajo bastante largo para recortar en la etiqueta", queued: i % 7, advisor: i % 2 ? "opus" : "",
  }));
  const model = (n, extra) => Object.assign({ agents: agents(n), advisorOn: true, advisorLabel: "opus", consult: [] }, extra || {});

  scene.update(model(9));
  const live9 = live.size;
  assert.ok(live9 > 0, "the resource counter saw nothing created");
  scene.update(model(9)); // the same model again must be harmless
  assert.strictEqual(live.size, live9, "an identical update changed the number of live resources");

  // Taps: some points of the screen hit an agent, and only a real agent is reported.
  const ptr = (x, y, extra) => Object.assign({ clientX: x, clientY: y, isPrimary: true, pointerId: 1, pointerType: "mouse", button: 0 }, extra || {});
  const tap = (x, y, extra) => {
    emit(renderer.domElement, "pointerdown", ptr(x, y, extra));
    emit(renderer.domElement, "pointerup", ptr(x, y, extra));
  };
  let hitAt = null;
  for (let x = 50; x < 1000 && !hitAt; x += 25) {
    for (let y = 30; y < 600 && !hitAt; y += 25) {
      const before = picks.length;
      tap(x, y);
      if (picks.length > before) hitAt = { x, y };
    }
  }
  assert.ok(hitAt, "no tap on the screen picked an agent");
  assert.ok(picks.every((id) => /^a[0-8]$/.test(id)), "a tap reported something that is not an agent: " + picks);
  const n = picks.length;
  const { x: hx, y: hy } = hitAt;
  tap(hx, hy, { isPrimary: false });
  tap(hx, hy, { button: 2 });
  emit(renderer.domElement, "pointerdown", ptr(hx, hy));
  emit(renderer.domElement, "pointercancel", ptr(hx, hy));
  emit(renderer.domElement, "pointerup", ptr(hx, hy));
  emit(renderer.domElement, "pointerdown", ptr(hx, hy));
  emit(renderer.domElement, "pointerup", ptr(hx, hy, { pointerId: 2 }));
  emit(renderer.domElement, "pointerdown", ptr(hx, hy));
  emit(renderer.domElement, "pointerup", ptr(hx + 40, hy));
  assert.strictEqual(picks.length, n, "a non-primary, wrong-button, cancelled, mismatched or dragged pointer picked an agent");
  // Close the pointers the negative cases left open, as a browser would, so the
  // real OrbitControls starts the touch from a clean state.
  for (const id of [1, 1, 2]) emit(renderer.domElement, "pointercancel", ptr(hx, hy, { pointerId: id }));
  tap(hx, hy, { pointerType: "touch", pointerId: 7 });
  assert.strictEqual(picks.length, n + 1, "a touch tap did not pick");

  scene.update(model(5, { consult: ["a0", "nobody"] })); // agents leave; a pulse starts; an unknown id is ignored
  scene.update(model(14)); // more than the scene draws: it keeps the first twelve
  scene.update({ agents: [], advisorOn: false, advisorLabel: "" });
  scene.update(model(3, { advisorOn: false }));
  drain();
  // Growing and shrinking again and again must not leave anything behind.
  scene.update(model(9));
  const histogram = () => {
    const h = {};
    for (const o of live) h[o.kind] = (h[o.kind] || 0) + 1;
    return h;
  };
  const settledHistogram = histogram();
  const settled = live.size;
  for (let i = 0; i < 6; i++) {
    scene.update(model(2, { consult: ["a1"] }));
    scene.update(model(9));
  }
  drain();
  assert.strictEqual(live.size, settled, "GPU resources grew over repeated updates: " + settled + " -> " + live.size +
    "\n  before " + JSON.stringify(settledHistogram) + "\n  after  " + JSON.stringify(histogram()));

  // The resize callback, then frames: the loop when motion is welcome, requested frames otherwise.
  stats.ro.forEach((o) => o.cb());
  if (reduceMotion) {
    assert.strictEqual(renderer.loop, undefined, "an animation loop runs although motion is reduced");
    for (let i = 0; i < 4 && frames.length; i++) frames.shift()();
  } else {
    assert.strictEqual(typeof renderer.loop, "function", "the animation loop did not start");
    for (let i = 0; i < 30; i++) renderer.loop();
  }
  assert.ok(renderer.frames > 0, "nothing was rendered");
  if (reduceMotion) {
    // A consult under reduced motion flashes the room and settles, with no dot left in flight.
    const live0 = live.size;
    scene.update(model(9, { consult: ["a1"] }));
    assert.strictEqual(live.size, live0, "a travelling dot was created although motion is reduced");
    const pending = Array.from(timers.values());
    assert.ok(pending.length >= 1, "the advisor flash was not scheduled");
    fire(() => true);
  }

  // Pause stops the loop; resuming starts it again (unless motion is reduced).
  scene.setPaused(true);
  if (!reduceMotion) assert.strictEqual(renderer.loop, null, "pausing left the loop running");
  const livePaused = live.size;
  scene.update(model(9, { consult: ["a1"] })); // while paused a pulse must not freeze mid-way
  assert.strictEqual(live.size, livePaused, "a travelling dot was created while paused");
  scene.setPaused(false);
  scene.resetCamera();
  // Hiding the tab stops it too, and so does the stage leaving the screen.
  document.hidden = true;
  emit(document, "visibilitychange", {});
  if (!reduceMotion) assert.strictEqual(renderer.loop, null, "a hidden tab kept drawing");
  document.hidden = false;
  emit(document, "visibilitychange", {});
  if (!reduceMotion) assert.strictEqual(typeof renderer.loop, "function", "showing the tab again did not restart the loop");
  stats.io.forEach((o) => o.cb([{ isIntersecting: false }]));
  if (!reduceMotion) assert.strictEqual(renderer.loop, null, "an off-screen stage kept drawing");
  stats.io.forEach((o) => o.cb([{ isIntersecting: true }]));
  if (!reduceMotion) assert.strictEqual(typeof renderer.loop, "function", "coming back on screen did not restart the loop");

  // Losing the graphics context shows a notice and waits; getting it back clears it.
  let prevented = false;
  emit(renderer.domElement, "webglcontextlost", { preventDefault() { prevented = true; } });
  assert.ok(prevented && notices.length >= 1 && notices[notices.length - 1] !== "", "a lost context was not announced");
  assert.strictEqual(lost.length, 0, "the scene gave up as soon as the context was lost");
  if (!reduceMotion) assert.strictEqual(renderer.loop, null, "the loop kept running with no context");
  emit(renderer.domElement, "webglcontextrestored", {});
  assert.strictEqual(notices[notices.length - 1], "", "the notice stayed after the context came back");
  assert.strictEqual(Array.from(timers.values()).filter((t) => t.ms === 5000).length, 0, "the give-up timer survived the restore");
  // If it does not come back in time, the scene gives up exactly once.
  emit(renderer.domElement, "webglcontextlost", { preventDefault() {} });
  fire((tm) => tm.ms === 5000);
  assert.strictEqual(lost.length, 1, "the scene did not give up when the context never came back");

  scene.dispose();
  scene.dispose(); // idempotent
  assert.ok(renderer.disposed && renderer.domElement.removed, "dispose left the renderer or the canvas behind");
  assert.ok([...stats.io, ...stats.ro].every((o) => o.gone), "an observer was left running");
  assert.strictEqual((document.listeners.visibilitychange || []).length, 0, "the visibility listener leaked");
  for (const t of ["pointerdown", "pointerup", "pointercancel", "pointermove", "webglcontextlost", "webglcontextrestored"]) {
    assert.strictEqual((renderer.domElement.listeners[t] || []).length, 0, t + " listener leaked");
  }
  assert.strictEqual(timers.size, 0, "a timer survived dispose");
  assert.strictEqual(live.size, 0, "GPU resources left after dispose: " + Array.from(live).map((o) => o.kind).join(","));
  const before = renderer.frames;
  while (frames.length) frames.shift()();
  assert.strictEqual(renderer.frames, before, "a frame was drawn after dispose");
  assert.deepStrictEqual(warnings, [], "Three.js or the scene logged warnings: " + warnings.join(" | "));
}

// ------------------------------------------------------------------- view --

function find(el, pred, out) {
  out = out || [];
  if (pred(el)) out.push(el);
  for (const c of el.children || []) find(c, pred, out);
  return out;
}

async function runView() {
  const t0 = Date.parse("2026-10-09T18:00:00Z");
  const state = { now: t0 };
  const jobs = [];
  const agents = [
    { id: "a0", name: "Ada", emoji: "🏛️", color: "#8b5cf6", enabled: true, advisor: "opus" },
    { id: "a1", name: "Linus", emoji: "🛠️", color: "#22c55e", enabled: true, advisor: "opus" },
    { id: "a2", name: "Kent", emoji: "🧪", color: "#06b6d4", enabled: true, advisor: "" },
    { id: "a3", name: "Guido", emoji: "🤖", color: "#3b82f6", enabled: false, advisor: "" },
  ];
  const advisorOf = (id) => (agents.find((a) => a.id === id) || {}).advisor;
  const job = (id, agent, status, extra) =>
    Object.assign({ id, agent_id: agent, status, title: "Trabajo " + id, agent: { advisor: advisorOf(agent) } }, extra || {});

  const build = (tag, props, ...children) => {
    const el = fakeElement(tag);
    const plain = props && !Array.isArray(props) && typeof props === "object" && !props.tag;
    if (plain) Object.assign(el.attrs, props);
    el.children = (plain ? children : [props, ...children]).flat(Infinity).filter((c) => c && typeof c === "object");
    el.children.forEach((c) => { c.parent = el; });
    return el;
  };
  const O = {
    state: { snap: {} }, now: () => state.now, u: { ms: (v) => Date.parse(v) },
    isRunning: (s) => s === "preparando" || s === "en_curso" || s === "limpiando",
    isFinished: (s) => s === "hecho" || s === "fallido" || s === "cancelado",
    icon: () => fakeElement("svg"),
    h: build,
    put(el, ...children) {
      el.putCount++;
      el.children = children.flat(Infinity).filter((c) => c && typeof c === "object");
      el.children.forEach((c) => { c.parent = el; });
      return el;
    },
    sel: {
      agents: () => agents,
      jobs: () => jobs,
      agentState(id) {
        const st = { state: "idle", job: null, queued: [], attention: [], last: null };
        for (const j of jobs) {
          if (j.agent_id !== id) continue;
          if (O.isRunning(j.status)) st.job = st.job || j;
          else if (j.status === "en_cola") st.queued.push(j);
        }
        st.state = st.job ? "working" : st.queued.length ? "queued" : "idle";
        return st;
      },
    },
    ui: {
      pageHead: () => fakeElement("header"),
      btn(label, o) { const b = fakeElement("button"); b.label = label; b.onClick = o && o.onClick; return b; },
    },
  };
  let route = null;
  O.route = (p, v) => { route = { path: p, view: v }; };
  const created = [];
  const sceneApi = {
    create(host, opts) {
      const s = { updates: [], disposed: false, opts, update(m) { this.updates.push(m); }, resetCamera() {}, setPaused() {}, dispose() { this.disposed = true; } };
      created.push(s);
      return s;
    },
  };
  const ctx = vm.createContext({
    document: { querySelector: () => null }, console, location: { hash: "" }, matchMedia: () => ({ matches: false }),
  });
  ctx.window = ctx;
  ctx.Oficina = O;
  ctx.Oficina3D = sceneApi;
  vm.runInContext(VIEW_SRC, ctx, { filename: "51-office3d.js" });
  assert.ok(route && route.path === "/3d", "the view did not register #/3d");
  const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

  // At mount: one job running, one queued for an agent with an advisor, and one that already finished.
  jobs.push(job("j1", "a1", "en_curso"), job("j2", "a0", "en_cola"),
    job("j0", "a0", "hecho", { finished: new Date(t0 - 5000).toISOString(), model_cost: { haiku: 0.1, opus: 0.3 } }));
  const rootEl = fakeElement("div");
  const inst = route.view.mount(rootEl, {}, new URLSearchParams());
  await flush();
  assert.strictEqual(created.length, 1, "the scene was not created");
  const scene = created[0];
  assert.strictEqual(scene.updates.length, 1, "the scene did not get its first model");
  const first = scene.updates[0];
  assert.strictEqual(JSON.stringify(first.agents.map((a) => a.id)), '["a0","a1","a2"]', "only active agents get a desk");
  assert.strictEqual(first.agents.find((a) => a.id === "a1").state, "working");
  assert.strictEqual(first.agents.find((a) => a.id === "a0").queued, 1);
  assert.strictEqual(JSON.stringify(first.consult), "[]", "a job that finished before the view opened pulsed");
  assert.strictEqual(first.advisorOn, true, "a working agent with an advisor must light the advisor's room");
  assert.strictEqual(first.advisorLabel, "opus");

  // The same data again: no model for the scene and no new list.
  const list = () => find(rootEl, (e) => e.tag === "ul")[0];
  const putsBefore = list().putCount;
  inst.update({}); inst.update({}); inst.update({});
  assert.strictEqual(scene.updates.length, 1, "an unchanged store made the scene redraw");
  assert.strictEqual(list().putCount, putsBefore, "an unchanged store rebuilt the list");

  // The queued job (it had NOT finished at mount) now finishes having used the advisor: it pulses once.
  const j2 = jobs.find((j) => j.id === "j2");
  Object.assign(j2, { status: "hecho", finished: new Date(state.now - 2000).toISOString(), model_cost: { haiku: 0.1, opus: 0.4 } });
  inst.update({});
  assert.strictEqual(scene.updates.length, 2, "a job that finished did not reach the scene");
  assert.strictEqual(JSON.stringify(scene.updates[1].consult), '["a0"]', "a queued job that finished with advisor cost did not pulse");
  inst.update({}); inst.update({});
  assert.strictEqual(scene.updates.length, 2, "the same finished job pulsed again");

  // A job whose cost lists a single model, or whose agent had no advisor, never pulses.
  jobs.push(job("j3", "a0", "hecho", { finished: new Date(state.now - 1000).toISOString(), model_cost: { haiku: 0.2 } }));
  jobs.push(job("j4", "a2", "hecho", { finished: new Date(state.now - 1000).toISOString(), model_cost: { haiku: 0.2, opus: 0.1 } }));
  inst.update({});
  assert.ok(scene.updates.every((u, i) => i === 1 || u.consult.length === 0), "a job without an advisor pulsed");
  // A running job that has not finished does not pulse either, and an old finished one is outside the window.
  jobs.push(job("j5", "a1", "en_curso"));
  jobs.push(job("j6", "a0", "hecho", { finished: new Date(state.now - 10 * 60 * 1000).toISOString(), model_cost: { haiku: 0.2, opus: 0.1 } }));
  inst.update({});
  assert.ok(scene.updates.every((u, i) => i === 1 || u.consult.length === 0), "an unfinished or old job pulsed");

  // The list links to each agent's jobs, with the id encoded.
  const hrefs = find(rootEl, (e) => typeof e.attrs.href === "string").map((e) => e.attrs.href);
  assert.ok(hrefs.includes("#/tablero?agente=a1"), "the agent list does not link to the board");

  // A lost graphics context shows a notice; getting it back clears it.
  const note = () => find(rootEl, (e) => e.attrs.role === "status" && e.parent)[0];
  scene.opts.onNotice("perdido");
  assert.ok(note() && /perdido/.test(note().textContent), "the notice was not shown");
  scene.opts.onNotice("");
  assert.ok(!note(), "the notice stayed after it was cleared");
  inst.unmount();
  assert.ok(scene.disposed, "unmount left the scene alive");

  // Leaving before the bundle loaded must not create a scene afterwards.
  const before = created.length;
  const inst2 = route.view.mount(fakeElement("div"), {}, new URLSearchParams());
  inst2.unmount();
  await flush();
  assert.strictEqual(created.length, before, "a scene was created after the view had gone");

  // Without WebGL the view says so and keeps the list.
  sceneApi.create = () => null;
  const root3 = fakeElement("div");
  const inst3 = route.view.mount(root3, {}, new URLSearchParams());
  await flush();
  const status = find(root3, (e) => e.attrs.role === "status" && e.parent)[0];
  assert.ok(status && /escena 3D/.test(status.textContent), "the no-WebGL message is missing");
  assert.ok(find(root3, (e) => typeof e.attrs.href === "string").length >= 3, "the list disappeared without WebGL");
  inst3.unmount();
}

(async () => {
  runScene({ reduceMotion: false, width: 1000 });
  runScene({ reduceMotion: true, width: 1000 });
  runScene({ reduceMotion: false, width: 500 });
  await runView();

  // Without WebGL the scene reports it by returning null.
  const ctx = vm.createContext({
    document: { createElement: (t) => fakeElement(t), addEventListener() {}, removeEventListener() {} }, navigator: {}, console,
    matchMedia: () => ({ matches: false }), requestAnimationFrame() {}, performance: { now: () => 0 },
  });
  ctx.window = ctx;
  vm.runInContext(THREE_SRC, ctx);
  ctx.THREE = Object.assign({}, ctx.THREE, { WebGLRenderer: class { constructor() { throw new Error("no webgl"); } } });
  vm.runInContext(SCENE_SRC, ctx);
  assert.strictEqual(ctx.Oficina3D.create(fakeElement("div"), {}), null, "create must return null without WebGL");
  console.log("office3d smoke: ok");
})().catch((e) => {
  const lines = String(e && e.stack ? e.stack : e).split("\n");
  // Keep the message and the first frames; a minified source line is never useful.
  console.error(lines.filter((l, i) => i < 4 || l.length < 300).map((l) => l.slice(0, 900)).join("\n"));
  process.exit(1);
});
