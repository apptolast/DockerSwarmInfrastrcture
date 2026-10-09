/*
 * Oficina de agentes · Vista 3D: the office as a Three.js scene.
 *
 * Pure drawing, loaded on demand after 00-three.js: it knows nothing of the
 * store. Oficina3D.create(host, opts) returns null when WebGL is not
 * available, or { update(model), resetCamera(), setPaused(bool), dispose() }.
 *
 *   model.agents       [{ id, name, emoji, color, state, title, queued, advisor }]
 *                      state: working | attention | queued | idle | sleeping
 *   model.advisorLabel text on the advisor's room ("" when no agent has one)
 *   model.advisorOn    the room lights up while a working agent has an advisor
 *   model.consult      agent ids whose finished job consulted the advisor: a
 *                      pulse travels from their desk to the room once
 *   opts.onPick(id)    an agent was clicked or tapped
 *   opts.onNotice(m)   a passing notice ("" clears it): the context was lost and
 *                      the browser may still give it back
 *   opts.onFallback(m) the scene cannot continue (the context did not come back)
 */
(() => {
  "use strict";
  const T = window.THREE;
  if (!T) return;

  const C = {
    ink: 0x12110e, floor: 0x1d1a15, room: 0x262018, wood: 0x3a2f25, metal: 0x2a2722,
    skin: 0xd6a57c, paper: 0xefe8d8, sage: 0x8fcfa4, lamp: 0xf0b35e, coral: 0xea7b68,
    violet: 0xa797ff, graphite: 0x5d5950, dim: 0x3b3830, off: 0x0b0a08,
  };
  const STATE = {
    working: { ring: C.sage, screen: C.sage },
    attention: { ring: C.coral, screen: C.coral },
    queued: { ring: C.lamp, screen: C.lamp },
    idle: { ring: C.graphite, screen: C.graphite },
    sleeping: { ring: C.dim, screen: C.off },
  };
  const COLS = 3;
  const GAP_X = 4.6;
  const GAP_Z = 4.4;
  const MAX_AGENTS = 12;
  const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

  const mat = (color, extra) => new T.MeshStandardMaterial(Object.assign({ color, roughness: 0.8, metalness: 0.05 }, extra || {}));
  const box = (w, h, d, material, shadow) => {
    const m = new T.Mesh(new T.BoxGeometry(w, h, d), material);
    m.castShadow = !!shadow;
    m.receiveShadow = true;
    return m;
  };
  const put = (o, x, y, z) => {
    o.position.set(x, y, z);
    return o;
  };

  function colorOf(value, fallback) {
    try {
      return new T.Color(typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value) ? value : fallback);
    } catch (e) {
      return new T.Color(fallback);
    }
  }

  // A text sprite drawn on a canvas; redrawn only when its lines change.
  function makeLabel(width) {
    const canvas = document.createElement("canvas");
    canvas.width = 512;
    canvas.height = 128;
    const texture = new T.CanvasTexture(canvas);
    texture.colorSpace = T.SRGBColorSpace;
    const sprite = new T.Sprite(new T.SpriteMaterial({ map: texture, transparent: true, depthWrite: false }));
    sprite.scale.set(width, width / 4, 1);
    return { sprite, canvas, texture, key: "" };
  }

  function drawLabel(label, lines) {
    const key = JSON.stringify(lines);
    if (label.key === key) return;
    label.key = key;
    const ctx = label.canvas.getContext("2d");
    ctx.clearRect(0, 0, 512, 128);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const gap = lines.length > 1 ? 46 : 0;
    lines.forEach((ln, i) => {
      ctx.font = "500 " + ln.size + "px " + MONO;
      ctx.fillStyle = ln.color;
      ctx.fillText(ln.text, 256, 64 - gap / 2 + i * gap);
    });
    label.texture.needsUpdate = true;
  }

  const hex = (n) => "#" + n.toString(16).padStart(6, "0");
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  function disposeTree(root) {
    root.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      const m = o.material;
      if (m) {
        for (const x of Array.isArray(m) ? m : [m]) {
          if (x.map) x.map.dispose();
          x.dispose();
        }
      }
    });
  }

  function create(host, opts) {
    const options = opts || {};
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const small = host.clientWidth < 600 || (navigator.hardwareConcurrency || 4) <= 2;

    let renderer;
    try {
      renderer = new T.WebGLRenderer({ antialias: !small && (window.devicePixelRatio || 1) < 2, powerPreference: "low-power" });
    } catch (e) {
      return null;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = !small;
    // The agents hardly move, so the shadow map is redrawn when the scene
    // changes (see update) and not on every frame.
    renderer.shadowMap.autoUpdate = false;
    renderer.shadowMap.needsUpdate = true;
    const canvas = renderer.domElement;
    canvas.setAttribute("aria-hidden", "true");
    host.prepend(canvas);

    const scene = new T.Scene();
    scene.background = new T.Color(C.ink);
    scene.fog = new T.Fog(C.ink, 34, 70);
    const camera = new T.PerspectiveCamera(42, 1, 0.1, 120);
    const controls = new T.OrbitControls(camera, canvas);
    controls.enableDamping = !reduce;
    controls.minDistance = 7;
    controls.maxDistance = 60;
    controls.maxPolarAngle = Math.PI * 0.46;

    scene.add(new T.HemisphereLight(0xfff1dc, 0x1a1712, 1.6));
    const key = new T.DirectionalLight(0xffe2b8, 2.2);
    key.position.set(-8, 14, 6);
    key.castShadow = !small;
    key.shadow.mapSize.set(1024, 1024);
    Object.assign(key.shadow.camera, { left: -16, right: 16, top: 16, bottom: -16, near: 1, far: 50 });
    scene.add(key);

    const floor = new T.Mesh(new T.PlaneGeometry(44, 34), mat(C.floor, { roughness: 0.95 }));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);
    const rug = new T.Mesh(new T.PlaneGeometry(15, 15), mat(0x201c16, { roughness: 1 }));
    rug.rotation.x = -Math.PI / 2;
    rug.position.set(0, 0.01, -0.5);
    rug.receiveShadow = true;
    scene.add(rug);

    // The advisor's glass room, lit while a working agent can consult it.
    const room = new T.Group();
    room.position.set(11.5, 0, -5);
    scene.add(room);
    const roomFloor = new T.Mesh(new T.PlaneGeometry(6, 6), mat(C.room));
    roomFloor.rotation.x = -Math.PI / 2;
    roomFloor.position.y = 0.012;
    roomFloor.receiveShadow = true;
    room.add(roomFloor);
    const glass = new T.MeshStandardMaterial({ color: 0xc9d4ff, transparent: true, opacity: 0.12, roughness: 0.15, depthWrite: false });
    room.add(put(new T.Mesh(new T.BoxGeometry(6, 2.8, 0.08), glass), 0, 1.4, -3));
    room.add(put(new T.Mesh(new T.BoxGeometry(0.08, 2.8, 6), glass), -3, 1.4, 0));
    room.add(put(new T.Mesh(new T.BoxGeometry(0.08, 2.8, 6), glass), 3, 1.4, 0));
    const advisorMat = new T.MeshStandardMaterial({ color: C.violet, emissive: C.violet, emissiveIntensity: 0.1, roughness: 0.5 });
    const advisorBody = put(new T.Mesh(new T.CapsuleGeometry(0.22, 0.42, 4, 12), advisorMat), 0, 1.05, -0.4);
    const advisorHead = put(new T.Mesh(new T.SphereGeometry(0.19, 16, 12), mat(C.skin)), 0, 1.72, -0.4);
    const advisorLight = new T.PointLight(C.violet, 1.2, 10, 1.6);
    advisorLight.position.set(0, 2.4, 0);
    const advisorLabel = makeLabel(4.4);
    advisorLabel.sprite.position.set(0, 2.5, -0.4);
    room.add(advisorBody, advisorHead, advisorLight, advisorLabel.sprite);
    let advisorOn = false;
    let glow = 0;
    const advisorHeadWorld = new T.Vector3();
    advisorHead.getWorldPosition(advisorHeadWorld);

    const agents = new Map();
    const hits = [];
    const links = new Map();
    const pulses = [];

    function buildAgent(m) {
      const g = new T.Group();
      const ink = colorOf(m.color, "#8b5cf6");
      g.add(put(box(2.2, 0.1, 1.1, mat(C.wood), true), 0, 0.95, 0));
      for (const [lx, lz] of [[-1, -0.45], [1, -0.45], [-1, 0.45], [1, 0.45]]) g.add(put(box(0.08, 0.95, 0.08, mat(C.metal)), lx, 0.475, lz));
      g.add(put(box(0.95, 0.6, 0.05, mat(C.metal), true), 0, 1.5, -0.35));
      const screenMat = new T.MeshBasicMaterial({ color: C.graphite });
      g.add(put(new T.Mesh(new T.PlaneGeometry(0.85, 0.5), screenMat), 0, 1.5, -0.32));
      g.add(put(box(0.6, 0.5, 0.6, mat(C.metal), true), 0, 0.25, 0.6));
      const body = put(new T.Mesh(new T.CapsuleGeometry(0.2, 0.4, 4, 12), mat(ink)), 0, 1.05, 0.6);
      body.castShadow = true;
      const head = put(new T.Mesh(new T.SphereGeometry(0.18, 16, 12), mat(C.skin)), 0, 1.7, 0.6);
      head.castShadow = true;
      const ringMat = new T.MeshBasicMaterial({ color: C.graphite, transparent: true, opacity: 0.85 });
      const ring = put(new T.Mesh(new T.TorusGeometry(1.55, 0.04, 8, 48), ringMat), 0, 0.03, 0.2);
      ring.rotation.x = Math.PI / 2;
      const papers = [];
      for (let i = 0; i < 5; i++) {
        const p = put(box(0.5, 0.02, 0.36, new T.MeshStandardMaterial({ color: C.paper, roughness: 0.9 })), 0.72, 1.015 + i * 0.024, 0.1);
        p.rotation.y = 0.12 * (i - 2);
        p.visible = false;
        papers.push(p);
        g.add(p);
      }
      const flagLabel = makeLabel(0.9);
      drawLabel(flagLabel, [{ text: "!", size: 64, color: hex(C.coral) }]);
      flagLabel.sprite.position.set(0, 2.55, 0.6);
      flagLabel.sprite.visible = false;
      const label = makeLabel(3.8);
      label.sprite.position.set(0, 2.3, 0.6);
      g.add(body, head, ring, flagLabel.sprite, label.sprite);
      scene.add(g);
      // Taps hit a box around the whole desk, not only the small avatar.
      const proxy = put(new T.Mesh(new T.BoxGeometry(2.4, 2.4, 2.2), new T.MeshBasicMaterial({ visible: false })), 0, 1.2, 0.3);
      g.add(proxy);
      for (const part of [body, head, proxy]) {
        part.userData.agentId = m.id;
        hits.push(part);
      }
      return { id: m.id, group: g, body, head, proxy, colorKey: m.color, ring, ringMat, screenMat, papers, label, flagLabel, state: "idle", phase: (agents.size + 1) * 1.3, order: 0, hasAdvisor: false };
    }

    function removeAgent(a) {
      scene.remove(a.group);
      for (const part of [a.body, a.head, a.proxy]) {
        const i = hits.indexOf(part);
        if (i >= 0) hits.splice(i, 1);
      }
      dropLink(a.id);
      disposeTree(a.group);
    }

    function dropLink(id) {
      const l = links.get(id);
      if (!l) return;
      scene.remove(l);
      disposeTree(l);
      links.delete(id);
    }

    function place() {
      const list = Array.from(agents.values()).sort((a, b) => a.order - b.order);
      const n = list.length;
      const rows = Math.max(1, Math.ceil(n / COLS));
      list.forEach((a, i) => {
        const col = i % COLS;
        const row = Math.floor(i / COLS);
        const perRow = Math.min(COLS, n - row * COLS);
        a.group.position.set((col - (perRow - 1) / 2) * GAP_X, 0, (row - (rows - 1) / 2) * GAP_Z - 0.5);
        dropLink(a.id); // its line is redrawn from the new place
      });
      lastRows = rows;
      home = homeFor(rows, camera.aspect);
      if (!touched) resetCamera();
    }

    function paint(a, m) {
      const st = STATE[m.state] ? m.state : "idle";
      const s = STATE[st];
      a.state = st;
      if (a.colorKey !== m.color) {
        a.colorKey = m.color;
        a.body.material.color.copy(colorOf(m.color, "#8b5cf6"));
      }
      a.ringMat.color.setHex(s.ring);
      a.screenMat.color.setHex(s.screen);
      a.flagLabel.sprite.visible = st === "attention";
      const n = Math.max(0, Math.min(5, m.queued || 0));
      a.papers.forEach((p, i) => { p.visible = i < n; });
      drawLabel(a.label, [{ text: clip((m.emoji || "🤖") + " " + (m.name || m.id), 22), size: 38, color: hex(C.paper) }]
        .concat(st === "working" && m.title ? [{ text: clip(m.title, 30), size: 26, color: hex(C.sage) }]
          : m.queued ? [{ text: m.queued + " en cola", size: 26, color: hex(C.lamp) }] : []));
      a.hasAdvisor = st === "working" && !!m.advisor;
      if (a.hasAdvisor && !links.has(a.id)) {
        const from = a.group.position.clone().setY(1.8);
        const to = new T.Vector3(11.5, 1.72, -5.4);
        const line = new T.Line(new T.BufferGeometry().setFromPoints([from, to]),
          new T.LineDashedMaterial({ color: C.violet, dashSize: 0.25, gapSize: 0.2, transparent: true, opacity: 0.35 }));
        line.computeLineDistances();
        scene.add(line);
        links.set(a.id, line);
      } else if (!a.hasAdvisor) {
        dropLink(a.id);
      }
    }

    const timers = new Set();
    function later(ms, fn) {
      const id = setTimeout(() => { timers.delete(id); fn(); }, ms);
      timers.add(id);
    }
    function pulse(a) {
      if (reduce || paused) {
        // Frames are drawn only on change here, so a travelling dot would
        // freeze mid-way: the advisor's room just lights up for a moment.
        glow = 1;
        request();
        later(1200, () => { glow = 0; request(); });
        return;
      }
      const from = a.group.position.clone().setY(1.8);
      const dot = new T.Mesh(new T.SphereGeometry(0.13, 12, 8), new T.MeshBasicMaterial({ color: C.violet, transparent: true }));
      dot.position.copy(from);
      scene.add(dot);
      pulses.push({ dot, from, age: 0 });
      glow = 1;
    }

    // The home view fits the desks and the room in both the wide and the tall
    // stage: a narrow one moves the camera back.
    function homeFor(rows, aspect) {
      const k = Math.min(2.2, Math.max(1, 1.5 / Math.max(aspect, 0.3)));
      const target = new T.Vector3(3, 0.8, -1);
      const base = new T.Vector3(13 + rows, 9 + rows * 2.2, 12 + rows * 2.4);
      return { position: target.clone().add(base.sub(target).multiplyScalar(k)), target };
    }
    let lastRows = 1;
    let touched = false;
    let home = homeFor(1, 1.6);
    controls.addEventListener("start", () => { touched = true; });
    function resetCamera() {
      touched = false;
      camera.position.copy(home.position);
      controls.target.copy(home.target);
      controls.update();
      request();
    }

    function update(model) {
      const list = ((model && model.agents) || []).slice(0, MAX_AGENTS);
      const keep = new Set(list.map((m) => m.id));
      let moved = false;
      for (const [id, a] of agents) {
        if (!keep.has(id)) {
          removeAgent(a);
          agents.delete(id);
          moved = true;
        }
      }
      list.forEach((m, i) => {
        let a = agents.get(m.id);
        if (!a) {
          a = buildAgent(m);
          agents.set(m.id, a);
          moved = true;
        }
        if (a.order !== i) moved = true;
        a.order = i;
      });
      if (moved) place();
      for (const m of list) paint(agents.get(m.id), m);
      renderer.shadowMap.needsUpdate = true;
      advisorOn = !!(model && model.advisorOn);
      drawLabel(advisorLabel, [{ text: model && model.advisorLabel ? "Consejero · " + clip(model.advisorLabel, 18) : "Sin consejero", size: 40, color: hex(C.violet) }]);
      for (const id of (model && model.consult) || []) {
        const a = agents.get(id);
        if (a) pulse(a);
      }
      request();
    }

    // ---- loop: continuous when motion is welcome, on demand when it is not.
    // Three.js 0.186 deprecates Clock: a plain frame timer is all the scene needs.
    let lastFrame = 0;
    let elapsed = 0;
    function frameDelta() {
      const now = performance.now();
      const dt = lastFrame ? Math.min((now - lastFrame) / 1000, 0.05) : 0;
      lastFrame = now;
      elapsed += dt;
      return dt;
    }
    let running = false;
    let paused = false;
    let visible = true;
    let queued = false;

    function step(dt, t) {
      for (const a of agents.values()) {
        if (a.state === "working" && !reduce) {
          a.body.position.y = 1.05 + Math.sin(t * 3 + a.phase) * 0.02;
          a.head.position.y = a.body.position.y + 0.65;
          a.screenMat.color.setHex(STATE.working.screen).multiplyScalar(0.8 + 0.2 * Math.sin(t * 5 + a.phase));
        } else if (a.state === "sleeping") {
          a.body.position.y = 1.0;
          a.head.position.y = 1.62;
        } else {
          a.body.position.y = 1.05;
          a.head.position.y = 1.7;
        }
      }
      glow = Math.max(0, glow - dt * 0.7);
      const target = (advisorOn ? 0.5 : 0.1) + glow * 1.1;
      advisorMat.emissiveIntensity += (target - advisorMat.emissiveIntensity) * Math.min(1, dt * 4 + (reduce ? 1 : 0));
      advisorLight.intensity = 1.2 + (advisorMat.emissiveIntensity - 0.1) * 8;
      for (let k = pulses.length - 1; k >= 0; k--) {
        const p = pulses[k];
        p.age += dt;
        p.dot.position.lerpVectors(p.from, advisorHeadWorld, Math.min(p.age / 1.6, 1));
        p.dot.material.opacity = Math.max(0, 1 - Math.max(0, p.age - 1.6) / 0.6);
        if (p.age > 2.2) {
          scene.remove(p.dot);
          disposeTree(p.dot);
          pulses.splice(k, 1);
        }
      }
      controls.update();
    }

    let lastRender = 0;
    function draw() {
      if (small && running && performance.now() - lastRender < 26) return; // about 38 fps on a phone
      lastRender = performance.now();
      step(frameDelta(), elapsed);
      renderer.render(scene, camera);
    }

    function start() {
      if (running || paused || !visible || reduce) return;
      running = true;
      lastFrame = 0;
      renderer.setAnimationLoop(draw);
    }
    function stop() {
      if (!running) return;
      running = false;
      renderer.setAnimationLoop(null);
    }
    // With reduced motion (or paused, or hidden) a frame is drawn only when something changes.
    function request() {
      if (running || queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        if (!disposed) draw();
      });
    }
    function refreshRunning() {
      if (!paused && visible && !document.hidden) start();
      else stop();
    }

    let disposed = false;
    const onVisibility = () => refreshRunning();
    document.addEventListener("visibilitychange", onVisibility);
    let io = null;
    if (typeof IntersectionObserver === "function") {
      io = new IntersectionObserver((entries) => {
        visible = entries.some((e) => e.isIntersecting);
        refreshRunning();
      });
      io.observe(host);
    }
    controls.addEventListener("change", request);

    const ro = new ResizeObserver(() => {
      const w = Math.max(1, host.clientWidth);
      const h = Math.max(1, host.clientHeight);
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.fov = w < 600 ? 58 : 42;
      camera.updateProjectionMatrix();
      home = homeFor(lastRows, camera.aspect);
      if (!touched) {
        camera.position.copy(home.position);
        controls.target.copy(home.target);
        controls.update();
      }
      request();
    });
    ro.observe(host);

    // ---- picking: a tap or click that did not drag.
    const ray = new T.Raycaster();
    const pointer = new T.Vector2();
    let down = null;
    function agentAt(e) {
      const r = canvas.getBoundingClientRect();
      pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(pointer, camera);
      const hit = ray.intersectObjects(hits, false)[0];
      return hit ? hit.object.userData.agentId : "";
    }
    const onDown = (e) => {
      if (!e.isPrimary || (e.pointerType === "mouse" && e.button !== 0)) return;
      down = { x: e.clientX, y: e.clientY, id: e.pointerId, touch: e.pointerType !== "mouse" };
    };
    const onCancel = () => { down = null; };
    const onUp = (e) => {
      const d = down;
      down = null;
      if (!d || d.id !== e.pointerId || Math.hypot(e.clientX - d.x, e.clientY - d.y) > (d.touch ? 10 : 5)) return;
      const id = agentAt(e);
      if (id && options.onPick) options.onPick(id);
    };
    const onMove = (e) => {
      if (e.pointerType === "mouse" && !down) canvas.style.cursor = agentAt(e) ? "pointer" : "grab";
    };
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointercancel", onCancel);
    canvas.addEventListener("pointermove", onMove);
    let lostTimer = null;
    const onLost = (e) => {
      e.preventDefault(); // asks the browser to give the context back
      stop();
      if (options.onNotice) options.onNotice("Se perdió el contexto gráfico; esperando a que el navegador lo recupere…");
      lostTimer = setTimeout(() => {
        lostTimer = null;
        if (options.onFallback) options.onFallback("No se recuperó el contexto gráfico del navegador.");
      }, 5000);
    };
    const onRestored = () => {
      clearTimeout(lostTimer);
      lostTimer = null;
      if (options.onNotice) options.onNotice("");
      renderer.shadowMap.needsUpdate = true;
      refreshRunning();
      request();
    };
    canvas.addEventListener("webglcontextlost", onLost);
    canvas.addEventListener("webglcontextrestored", onRestored);

    function dispose() {
      if (disposed) return;
      disposed = true;
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
      if (io) io.disconnect();
      ro.disconnect();
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointercancel", onCancel);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("webglcontextlost", onLost);
      canvas.removeEventListener("webglcontextrestored", onRestored);
      clearTimeout(lostTimer);
      for (const id of timers) clearTimeout(id);
      timers.clear();
      controls.dispose();
      disposeTree(scene);
      renderer.dispose();
      canvas.remove();
    }

    resetCamera();
    refreshRunning();
    return {
      update,
      resetCamera,
      setPaused(value) {
        paused = !!value;
        refreshRunning();
        if (paused) request();
      },
      dispose,
    };
  }

  window.Oficina3D = { create, revision: T.REVISION };
})();
