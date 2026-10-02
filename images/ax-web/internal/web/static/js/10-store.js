/*
 * Oficina de agentes · state and live updates.
 *
 * The store holds the last GET /api/office snapshot and applies the
 * `office` deltas of the single EventSource (/api/stream, with ?job= when
 * a job is being watched). The snapshot is fetched once at load and again
 * only on `invalidate` or after a reconnect that missed revisions,
 * debounced to at most one request per second. There is no polling.
 *
 * Events emitted on the bus:
 *   "store" {full, jobs:[ids]}  after a snapshot or a delta
 *   "log"   {jobId, ev}         a harness.Event of the watched job
 *   "conn"  state               "connecting" | "live" | "retrying"
 */
(() => {
  "use strict";
  const O = window.Oficina;
  const S = (O.state = {
    snap: null,
    jobs: new Map(),
    jobsVersion: 0,
    counts: null,
    conn: "connecting",
    loadError: null,
    github: new Map(), // project id -> {at, data}
  });

  const ARRAYS = ["agents", "projects", "jobs", "queue", "pipelines", "templates", "schedules",
    "proposals", "evals", "warnings"];

  function normalize(snap) {
    for (const k of ARRAYS) if (!Array.isArray(snap[k])) snap[k] = [];
    snap.settings = snap.settings || {};
    snap.catalog = snap.catalog || {};
    for (const h of ["claude", "codex"]) {
      const c = snap.catalog[h] || (snap.catalog[h] = {});
      if (!Array.isArray(c.models)) c.models = [];
      if (!Array.isArray(c.efforts)) c.efforts = [];
    }
    snap.metrics = snap.metrics || {};
    if (!Array.isArray(snap.metrics.by_agent_version)) snap.metrics.by_agent_version = [];
    if (!Array.isArray(snap.metrics.by_model)) snap.metrics.by_model = [];
    snap.ax = snap.ax || {};
    snap.credentials = snap.credentials || {};
    if (!Array.isArray(snap.credentials.github_owners)) snap.credentials.github_owners = [];
    snap.limits = snap.limits || {};
    for (const a of snap.agents) {
      if (!Array.isArray(a.history)) a.history = [];
      if (!Array.isArray(a.disallowed_tools)) a.disallowed_tools = [];
    }
    for (const p of snap.projects) if (!Array.isArray(p.memory)) p.memory = [];
    for (const p of snap.pipelines) {
      if (!Array.isArray(p.steps)) p.steps = [];
      p.participants = p.participants || {};
    }
    for (const t of snap.templates) if (!Array.isArray(t.roles)) t.roles = [];
    for (const s of snap.schedules) if (!Array.isArray(s.days)) s.days = [];
    return snap;
  }

  let snapId = 0;
  function setSnapshot(snap) {
    snapId++;
    S.snap = normalize(snap);
    S.jobs = new Map();
    for (const j of S.snap.jobs) S.jobs.set(j.id, j);
    S.jobsVersion++;
    S.loadError = null;
    const now = O.u.ms(S.snap.now);
    if (Number.isFinite(now)) O.clockOffset = now - Date.now();
    O.emit("store", { full: true, jobs: [] });
  }

  let lastFetch = 0, inflight = false, again = false, timer = null;

  async function fetchSnapshot() {
    inflight = true;
    lastFetch = Date.now();
    try {
      setSnapshot(await O.api.get("/api/office"));
      // The stream said hello with a newer revision before this arrived.
      // Checked once per hello, so mismatched counters can never loop.
      if (typeof S.helloRev === "number" && S.helloRev > S.snap.rev) again = true;
      S.helloRev = undefined;
    } catch (e) {
      S.loadError = e;
      if (!S.snap) O.emit("store", { full: true, jobs: [], error: e });
      else O.fail(e, "No se pudo actualizar la oficina");
    } finally {
      inflight = false;
      if (again) {
        again = false;
        refresh();
      }
    }
  }

  // While held (a batch of mutations), reloads wait and run once at the end.
  let held = 0, heldPending = false;

  /** Schedules a snapshot reload, at least one second after the last one. */
  function refresh() {
    if (held) {
      heldPending = true;
      return;
    }
    if (inflight) {
      again = true;
      return;
    }
    if (timer) return;
    const wait = Math.max(1000, lastFetch + 1000 - Date.now());
    timer = setTimeout(() => {
      timer = null;
      fetchSnapshot();
    }, wait);
  }

  function upsertJob(job) {
    if (!job || !job.id) return;
    if (job.deleted) S.jobs.delete(job.id);
    else S.jobs.set(job.id, job);
    S.jobsVersion++;
  }

  function applyDelta(d) {
    if (!S.snap || !d) return;
    if (typeof d.rev === "number") {
      if (d.rev <= S.snap.rev) return;
      S.snap.rev = d.rev;
    }
    const ids = [];
    if (Array.isArray(d.jobs)) {
      for (const j of d.jobs) {
        upsertJob(j);
        ids.push(j.id);
      }
    }
    if ("active" in d) S.snap.active = d.active || null;
    if (Array.isArray(d.queue)) S.snap.queue = d.queue;
    if (d.ax) S.snap.ax = d.ax;
    if (d.counts) S.counts = d.counts;
    if (Array.isArray(d.invalidate) && d.invalidate.length) refresh();
    O.emit("store", { full: false, jobs: ids });
  }

  O.store = {
    load: fetchSnapshot,
    refresh,
    /**
     * After a mutation: the server announces it with `invalidate` on the
     * stream, so only reload by hand when the stream is not live.
     */
    sync() {
      if (S.conn !== "live") refresh();
    },
    upsertJob(job) {
      upsertJob(job);
      O.emit("store", { full: false, jobs: [job.id] });
    },
    removeJob(id) {
      S.jobs.delete(id);
      S.jobsVersion++;
      O.emit("store", { full: false, jobs: [id] });
    },
    applyDelta,
    /**
     * Defers snapshot reloads (e.g. the invalidations a batch of creations
     * announces on the stream) until the returned release is called; then
     * one reload runs if any was asked for.
     */
    hold() {
      held++;
      let done = false;
      return () => {
        if (done) return;
        done = true;
        held--;
        if (!held && heldPending) {
          heldPending = false;
          refresh();
        }
      };
    },
  };

  // ------------------------------------------------------------ selectors --

  // Derived values are cached until the snapshot or a job changes.
  let memoKey = "", memo = {};
  function memoized(name, fn) {
    const key = snapId + ":" + S.jobsVersion + ":" + (S.snap ? S.snap.rev : 0);
    if (key !== memoKey) {
      memoKey = key;
      memo = {};
    }
    if (!(name in memo)) memo[name] = fn();
    return memo[name];
  }

  let sortedCache = null, sortedVersion = -1;
  const sel = {
    jobs() {
      if (sortedVersion !== S.jobsVersion) {
        sortedCache = Array.from(S.jobs.values()).sort((a, b) => (O.u.ms(b.created) || 0) - (O.u.ms(a.created) || 0));
        sortedVersion = S.jobsVersion;
      }
      return sortedCache;
    },
    job: (id) => S.jobs.get(id) || null,
    agents: () => (S.snap ? S.snap.agents : []),
    agent: (id) => (S.snap ? S.snap.agents.find((a) => a.id === id) || null : null),
    projects: () => (S.snap ? S.snap.projects : []),
    project: (id) => (S.snap ? S.snap.projects.find((p) => p.id === id) || null : null),
    pipeline: (id) => (S.snap ? S.snap.pipelines.find((p) => p.id === id) || null : null),
    template: (id) => (S.snap ? S.snap.templates.find((t) => t.id === id) || null : null),
    activeJob() {
      const a = S.snap && S.snap.active;
      return a && a.job_id ? sel.job(a.job_id) : null;
    },
    queueJobs() {
      if (!S.snap) return [];
      const q = S.snap.queue.map((id) => sel.job(id)).filter(Boolean);
      // Queued jobs that the queue list does not name yet, oldest last.
      const seen = new Set(S.snap.queue);
      for (const j of sel.jobs()) if (j.status === "en_cola" && !seen.has(j.id)) q.push(j);
      return q;
    },
    running: () => sel.jobs().filter((j) => O.isRunning(j.status)),
    pendingProposals: () => (S.snap ? S.snap.proposals.filter((p) => p.status === "pendiente") : []),
    runningPipelines: () => (S.snap ? S.snap.pipelines.filter((p) => p.status === "en_curso") : []),

    /** True when a finished job is waiting for a human verdict. */
    needsReview(j) {
      if (j.status !== "hecho" || j.kind !== "cambio" || j.rating || j.pr) return false;
      if (!j.changes || !(j.changes.files > 0)) return false;
      if (!j.pipeline_id) return true;
      const p = sel.pipeline(j.pipeline_id);
      if (!p) return true;
      if (p.status === "en_curso") return false;
      const changeSteps = p.steps.filter((s) => s.kind === "cambio" && s.job_id);
      return changeSteps.length > 0 && changeSteps[changeSteps.length - 1].job_id === j.id;
    },
    /** A failed job nobody looked at, not retried, from the last week. */
    needsRetry(j) {
      if (j.status !== "fallido" || j.rating) return false;
      const fin = O.u.ms(j.finished || j.created);
      if (Number.isFinite(fin) && O.now() - fin > 7 * 86400e3) return false;
      return !sel.retried().has(j.id);
    },
    /** Ids of failed jobs that a later job retried or replaced. */
    retried: () => memoized("retried", () => {
      const out = new Set();
      const failed = sel.jobs().filter((j) => j.status === "fallido");
      if (!failed.length) return out;
      const byKey = new Map();
      for (const k of sel.jobs()) {
        if (k.source && k.source.job_id) out.add(k.source.job_id);
        if (k.pipeline_id) continue;
        const key = k.title + "\u0000" + k.agent_id + "\u0000" + k.project_id;
        const t = O.u.ms(k.created) || 0;
        byKey.set(key, Math.max(byKey.get(key) || 0, t));
      }
      for (const j of failed) {
        const newest = byKey.get(j.title + "\u0000" + j.agent_id + "\u0000" + j.project_id) || 0;
        if (newest > (O.u.ms(j.created) || 0)) out.add(j.id);
      }
      return out;
    }),

    /** Everything that needs a human, newest first. */
    inbox: () => memoized("inbox", () => {
      if (!S.snap) return [];
      const items = [];
      for (const p of sel.pendingProposals()) items.push({ type: "proposal", key: "p:" + p.id, at: p.created, p });
      for (const j of sel.jobs()) {
        if (j.stalled && O.isRunning(j.status)) items.push({ type: "stalled", key: "s:" + j.id, at: j.started || j.created, j });
        else if (sel.needsReview(j)) items.push({ type: "review", key: "r:" + j.id, at: j.finished || j.created, j });
        else if (sel.needsRetry(j)) items.push({ type: "failed", key: "f:" + j.id, at: j.finished || j.created, j });
      }
      const ax = S.snap.ax || {};
      if (ax.cleanup_failed) items.push({ type: "cleanup", key: "c:" + ax.cleanup_failed, at: null, name: ax.cleanup_failed });
      if (ax.reap_note) items.push({ type: "note", key: "n:reap", at: null, text: ax.reap_note });
      for (const w of S.snap.warnings) items.push({ type: "note", key: "w:" + w, at: null, text: w });
      return items.sort((a, b) => (O.u.ms(b.at) || Infinity) - (O.u.ms(a.at) || Infinity));
    }),
    inboxCount: () => memoized("inboxCount", () => sel.inbox().filter((i) => i.type !== "note").length),

    /** The state of an agent's desk. */
    agentState: (agentId) => memoized("agent:" + agentId, () => {
      const st = { state: "idle", job: null, queued: [], attention: [], last: null };
      for (const j of sel.jobs()) {
        if (j.agent_id !== agentId) continue;
        if (O.isRunning(j.status)) st.job = st.job || j;
        else if (j.status === "en_cola") st.queued.push(j);
        else {
          if (!st.last) st.last = j;
          if (sel.needsReview(j) || sel.needsRetry(j)) st.attention.push(j);
        }
      }
      if (st.job) st.state = "working";
      else if (st.attention.length) st.state = "attention";
      else if (st.queued.length) st.state = "queued";
      else {
        const lastAt = st.last ? O.u.ms(st.last.finished || st.last.created) : NaN;
        st.state = Number.isFinite(lastAt) && O.now() - lastAt < 8 * 3600e3 ? "idle" : "sleeping";
      }
      return st;
    }),

    /** Counters of the whiteboard (today = local day). */
    today() {
      const start = new Date(O.now());
      start.setHours(0, 0, 0, 0);
      const t0 = start.getTime();
      let done = 0, cost = 0, failed = 0;
      for (const j of sel.jobs()) {
        const fin = O.u.ms(j.finished);
        if (!Number.isFinite(fin) || fin < t0) continue;
        if (j.status === "hecho") done++;
        if (j.status === "fallido") failed++;
        cost += (j.usage && j.usage.cost_usd) || 0;
      }
      return { done, failed, cost };
    },

    projectName: (id) => {
      const p = sel.project(id);
      return p ? p.name : id || "—";
    },
    githubReady(projectId) {
      const c = S.snap && S.snap.credentials;
      if (!c || !c.github) return false;
      const p = sel.project(projectId);
      if (!p) return false;
      const m = /^https:\/\/github\.com\/([^/]+)\//i.exec(p.repo || "");
      const owners = (c.github_owners || []).map((o) => String(o).toLowerCase());
      return owners.includes("*") || owners.includes("todas") || (m && owners.includes(m[1].toLowerCase()));
    },
  };
  O.sel = sel;

  /** GitHub issues and PRs of a project, cached for a minute. */
  O.github = async function github(projectId, force) {
    const c = S.github.get(projectId);
    if (!force && c && Date.now() - c.at < 60000) return c.data;
    const data = await O.api.get("/api/projects/" + O.enc(projectId) + "/github");
    data.issues = Array.isArray(data.issues) ? data.issues : [];
    data.pulls = Array.isArray(data.pulls) ? data.pulls : [];
    S.github.set(projectId, { at: Date.now(), data });
    return data;
  };

  // --------------------------------------------------------------- stream --

  const watchers = { route: null, drawer: null };
  let es = null;
  let current;            // job id of the open stream ("" = none)
  let retryTimer = null;
  let backoff = 0;
  let opened = false;
  let syncing = false;
  // Events of the watched job, kept so a second view of it starts full.
  const cache = { jobId: "", events: [], lastSeq: 0 };
  const MAX_CACHE = 5000;

  function setConn(state) {
    if (S.conn === state) return;
    S.conn = state;
    O.emit("conn", state);
  }

  function effective() {
    return watchers.drawer || watchers.route || "";
  }

  function parse(data) {
    try {
      return JSON.parse(data);
    } catch (e) {
      return null;
    }
  }

  function connect() {
    if (es) {
      es.close();
      es = null;
    }
    clearTimeout(retryTimer);
    retryTimer = null;
    current = effective();
    if (cache.jobId !== current) {
      cache.jobId = current;
      cache.events = [];
      cache.lastSeq = 0;
    }
    if (typeof EventSource === "undefined") return;
    setConn(S.conn === "live" ? "connecting" : S.conn);
    const src = new EventSource("/api/stream" + (current ? "?job=" + O.enc(current) : ""));
    es = src;
    src.addEventListener("hello", (e) => {
      if (es !== src) return;
      backoff = 0;
      setConn("live");
      const d = parse(e.data);
      if (d && typeof d.rev === "number") S.helloRev = d.rev;
      // A revision we have not seen means deltas were missed.
      if (S.snap && d && typeof d.rev === "number" && d.rev !== S.snap.rev) refresh();
      else if (S.snap && opened && (!d || typeof d.rev !== "number")) refresh();
      opened = true;
    });
    src.addEventListener("office", (e) => {
      if (es !== src) return;
      const d = parse(e.data);
      if (d) applyDelta(d);
    });
    src.addEventListener("log", (e) => {
      if (es !== src) return;
      const ev = parse(e.data);
      if (!ev) return;
      const jobId = ev.job || ev.job_id || current;
      if (jobId === cache.jobId) {
        if (typeof ev.seq === "number" && ev.seq > 0) {
          if (ev.seq <= cache.lastSeq) return;
          cache.lastSeq = ev.seq;
        }
        cache.events.push(ev);
        if (cache.events.length > MAX_CACHE) cache.events.splice(0, cache.events.length - MAX_CACHE);
      }
      O.emit("log", { jobId, ev });
    });
    src.onerror = () => {
      if (es !== src) return;
      if (src.readyState === 2) {
        // Closed for good (e.g. an HTTP error): reopen with backoff.
        src.close();
        es = null;
        setConn("retrying");
        backoff = Math.min(60000, backoff ? backoff * 2 : 5000);
        retryTimer = setTimeout(connect, backoff);
      } else {
        setConn("retrying");
      }
    };
  }

  O.stream = {
    start() {
      connect();
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && !es && !retryTimer) connect();
        if (document.visibilityState === "visible" && retryTimer && backoff > 5000) {
          backoff = 0;
          connect();
        }
      });
      window.addEventListener("online", () => {
        if (!es || es.readyState === 2) connect();
      });
    },
    /** watch(source, jobId): source is "route" or "drawer". */
    watch(source, jobId) {
      watchers[source] = jobId || null;
      if (current === undefined || syncing) return; // not started yet, or already scheduled
      // Coalesce changes made in one go (leaving a view and opening another).
      syncing = true;
      setTimeout(() => {
        syncing = false;
        if (effective() !== current) connect();
      }, 0);
    },
    /** Events already received for jobId (only for the watched job). */
    events: (jobId) => (cache.jobId === jobId ? cache.events.slice() : []),
    watching: () => current || "",
  };
})();
