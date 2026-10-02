package demo

import (
	"context"
	crand "crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"math/rand/v2"
	"regexp"
	"strings"
	"sync"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// Executor is a simulated harness.Executor: each run plays a plausible
// agent session (session start, text, tools and their results, a final
// result with usage and cost) over about 15 seconds divided by Speed, and
// answers each kind of job the way the office expects: a small patch for
// a change, alternating verdicts for reviews, a score for a judge and a
// JSON block for the coach. Nothing is executed.
type Executor struct {
	// Speed divides the duration of a run (1 = about 15 s).
	Speed float64
	// Credentials is called once per run like the real run manager does;
	// the values are discarded.
	Credentials func(harnessName string) (map[string]string, error)
	// SaveCodexAuth receives the "renewed" auth.json after a Codex run.
	SaveCodexAuth func(data []byte, created time.Time) error
	ax            *AX
	now           func() time.Time

	mu       sync.Mutex
	active   *run
	stopping bool
	reviews  int
	rnd      *rand.Rand
	wg       sync.WaitGroup
}

var _ harness.Executor = (*Executor)(nil)

// NewExecutor returns an executor that records its runs in ax.
func NewExecutor(speed float64, ax *AX, now func() time.Time) *Executor {
	if now == nil {
		now = time.Now
	}
	e := &Executor{Speed: speed, ax: ax, now: now, rnd: rand.New(rand.NewPCG(uint64(now().UnixNano()), 7))}
	if ax != nil {
		ax.active = e.ActiveTask
	}
	return e
}

type run struct {
	id      string
	spec    harness.Spec
	hooks   harness.Hooks
	done    chan struct{}
	cancel  chan struct{}
	once    sync.Once
	created time.Time
	res     harness.Result
}

func (r *run) ID() string             { return r.id }
func (r *run) Done() <-chan struct{}  { return r.done }
func (r *run) Result() harness.Result { return r.res }

// Launch starts a simulated run, one at a time.
func (e *Executor) Launch(_ context.Context, spec harness.Spec, _ string, hooks harness.Hooks) (harness.Run, error) {
	if err := harness.Validate(spec); err != nil {
		return nil, err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.stopping {
		return nil, harness.ErrNotReady
	}
	if e.active != nil {
		return nil, fmt.Errorf("%w (%s)", harness.ErrBusy, e.active.id)
	}
	r := &run{id: spec.ID, spec: spec, hooks: hooks, done: make(chan struct{}), cancel: make(chan struct{}), created: e.now()}
	e.active = r
	e.wg.Add(1)
	go e.play(r)
	return r, nil
}

// ActiveTask is the run in progress, or "".
func (e *Executor) ActiveTask() string {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.active != nil {
		return e.active.id
	}
	return ""
}

// Cancel stops the run with that id.
func (e *Executor) Cancel(id, _ string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.active != nil && e.active.id == id {
		e.active.once.Do(func() { close(e.active.cancel) })
	}
	return nil
}

// Ready is true until Shutdown.
func (e *Executor) Ready() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return !e.stopping
}

// Shutdown cancels the active run and waits for it until ctx ends.
func (e *Executor) Shutdown(ctx context.Context) {
	e.mu.Lock()
	e.stopping = true
	if e.active != nil {
		e.active.once.Do(func() { close(e.active.cancel) })
	}
	e.mu.Unlock()
	done := make(chan struct{})
	go func() {
		e.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
	}
}

func (e *Executor) intn(n int) int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.rnd.IntN(n)
}

// kinds the prompt asks for, recognised by the office's closing
// instructions.
func jobKind(spec harness.Spec) string {
	switch {
	case strings.Contains(spec.Prompt, "`VEREDICTO: APROBADO` o `VEREDICTO: CAMBIOS`"):
		return "revision"
	case strings.Contains(spec.Prompt, "`PUNTUACION: n`"):
		return "juez"
	case strings.Contains(spec.Prompt, `{"system_prompt":`):
		return "retro"
	case spec.CaptureChanges:
		return "cambio"
	case strings.Contains(spec.Prompt, "Entrega un plan numerado"):
		return "plan"
	}
	return "pregunta"
}

func ok(v bool) *bool { return &v }

// script is the session a run plays.
func (e *Executor) script(spec harness.Spec, kind string) []harness.Event {
	model := spec.Model
	if model == "" {
		model = map[string]string{harness.Claude: "claude-sonnet-5", harness.Codex: "gpt-6-sol"}[spec.Harness]
	}
	var evs []harness.Event
	if spec.Harness == harness.Codex {
		evs = append(evs, harness.Event{Kind: harness.EventInit, Model: model, Text: "Sesión de Codex iniciada"})
	} else {
		perm := "restringidos"
		if spec.Mode == harness.ModeFull {
			perm = "bypassPermissions"
		}
		evs = append(evs, harness.Event{Kind: harness.EventInit, Model: model,
			Text: fmt.Sprintf("Sesión iniciada (modelo %s, 23 herramientas, permisos %s)", model, perm)})
	}
	pool := [][]harness.Event{
		{{Kind: harness.EventThinking, Text: "Primero miro la estructura del repositorio y la documentación."}},
		{{Kind: harness.EventTool, Tool: "Bash", Input: "ls -la"},
			{Kind: harness.EventToolResult, OK: ok(true), Text: "README.md\ndocs\nscripts\nimages\n.github"}},
		{{Kind: harness.EventTool, Tool: "Read", Input: "README.md"},
			{Kind: harness.EventToolResult, OK: ok(true), Text: "# Proyecto\n\nCómo se instala y se despliega…"}},
		{{Kind: harness.EventTool, Tool: "Grep", Input: "TODO en ."},
			{Kind: harness.EventToolResult, OK: ok(true), Text: "scripts/deploy.sh:41: # TODO: validar la entrada"}},
		{{Kind: harness.EventTool, Tool: "Glob", Input: "**/*_test.go"},
			{Kind: harness.EventToolResult, OK: ok(true), Text: "internal/web/server_test.go\ninternal/office/office_test.go"}},
		{{Kind: harness.EventText, Text: "El punto de entrada está en main.go; la configuración se valida en internal/config."}},
		{{Kind: harness.EventTodo, Text: "[x] Leer la estructura\n[ ] Revisar los tests\n[ ] Redactar el resultado"}},
		{{Kind: harness.EventTool, Tool: "Bash", Input: "git log --oneline -5"},
			{Kind: harness.EventToolResult, OK: ok(true), Text: "bc39fb8 Ajusta la ventana del Observatorio\n…"}},
	}
	if spec.Mode == harness.ModeFull {
		pool = append(pool,
			[]harness.Event{{Kind: harness.EventTool, Tool: "Bash", Input: "go test ./..."},
				{Kind: harness.EventToolResult, OK: ok(true), Text: "ok  \tapptolast.com/ax-web/internal/office\t3.1s"}},
			[]harness.Event{{Kind: harness.EventTool, Tool: "Bash", Input: "make lint"},
				{Kind: harness.EventToolResult, OK: ok(false), Text: "make: *** No rule to make target 'lint'.  Stop."}})
	}
	if kind == "cambio" {
		pool = append(pool, []harness.Event{{Kind: harness.EventTool, Tool: "Edit", Input: "README.md"},
			{Kind: harness.EventToolResult, OK: ok(true), Text: "The file README.md has been updated."}})
	}
	n := 6 + e.intn(15)
	for len(evs) < n {
		evs = append(evs, pool[e.intn(len(pool))]...)
	}
	return evs
}

var personaFence = regexp.MustCompile("(?s)## Prompt de sistema actual\\n(`{3,})text\\n(.*?)\\n`{3,}")

// result is the final answer for a kind of job.
func (e *Executor) result(spec harness.Spec, kind string) string {
	var b strings.Builder
	switch kind {
	case "revision":
		e.mu.Lock()
		e.reviews++
		approved := e.reviews%2 == 0
		e.mu.Unlock()
		if approved {
			b.WriteString("He revisado el cambio aplicado en el árbol: cumple la tarea y los tests pasan.\n\n" +
				"- Baja · README.md:3 · la frase podría ser más corta (opcional).\n")
		} else {
			b.WriteString("He revisado el cambio aplicado en el árbol.\n\n" +
				"- Alta · scripts/deploy.sh:41 · falta validar la entrada antes de usarla en una orden; " +
				"arreglo: comprobarla con una expresión regular y salir con error.\n" +
				"- Media · README.md:12 · el ejemplo usa una ruta que no existe.\n")
		}
	case "juez":
		fmt.Fprintf(&b, "La respuesta cubre los criterios principales; le falta citar algunas líneas.\n")
	case "retro":
		current := "Eres un agente de la Oficina de AppToLast."
		if m := personaFence.FindStringSubmatch(spec.Prompt); m != nil {
			current = m[2]
		}
		proposal := strings.TrimSpace(current) + "\n- Antes de terminar, ejecuta los tests del proyecto y pega la orden y su resultado."
		data := fmt.Sprintf(`{"system_prompt": %q, "motivo": "En los últimos trabajos faltó la verificación (demo).", "cambios": ["Pide ejecutar y citar los tests"]}`, proposal)
		b.WriteString("He revisado las métricas y los últimos trabajos.\n\n```json\n" + data + "\n```\n")
	case "cambio":
		b.WriteString("He actualizado README.md con una sección de arranque en local y lo he comprobado.\n")
	case "plan":
		b.WriteString("1. Leer la configuración actual (config/).\n2. Añadir la validación en scripts/deploy.sh.\n" +
			"3. Cubrirla con un test.\n4. Documentarla en README.md.\nRiesgos: ninguno en producción; verificación: los tests.\n")
	default:
		b.WriteString("El punto de entrada está en main.go:30; la configuración se valida en internal/config/config.go:150.\n")
	}
	b.WriteString("\n## Resumen\n")
	switch kind {
	case "revision":
		b.WriteString("Revisión del cambio con sus hallazgos priorizados.\n")
	case "cambio":
		b.WriteString("README.md documenta cómo arrancar el proyecto en local.\n")
	default:
		b.WriteString("Respuesta simulada por la demo de la Oficina.\n")
	}
	b.WriteString("\n## Lecciones\n")
	if e.intn(3) == 0 {
		b.WriteString("- Los tests del proyecto se ejecutan con «go test ./...» desde la raíz.\n")
	} else {
		b.WriteString("- ninguna\n")
	}
	switch kind {
	case "revision":
		if strings.Contains(b.String(), "Alta ·") {
			b.WriteString("\nVEREDICTO: CAMBIOS\n")
		} else {
			b.WriteString("\nVEREDICTO: APROBADO\n")
		}
	case "juez":
		fmt.Fprintf(&b, "\nPUNTUACION: %d\n", 5+e.intn(5))
	}
	return b.String()
}

const readmeBefore = "# Proyecto\n\nDescripción del proyecto.\n"

// changes are the simulated edit of README.md.
func changes(spec harness.Spec, n int) *harness.Changes {
	extra := ""
	for i := range n {
		extra += fmt.Sprintf("- Paso %d: consulta docs/ antes de desplegar.\n", i+1)
	}
	after := readmeBefore + "\n## Arranque en local\n\n" + extra
	added := 3 + n
	patch := "diff --git a/README.md b/README.md\nindex 1111111..2222222 100644\n--- a/README.md\n+++ b/README.md\n" +
		fmt.Sprintf("@@ -1,3 +1,%d @@\n", 3+added) +
		" # Proyecto\n \n Descripción del proyecto.\n+\n+## Arranque en local\n+\n"
	for l := range strings.Lines(extra) {
		patch += "+" + l
	}
	var sha [20]byte
	_, _ = crand.Read(sha[:])
	return &harness.Changes{
		BaseSHA: hex.EncodeToString(sha[:]), Patch: []byte(patch),
		Files:            []harness.ChangedFile{{Path: "README.md", Status: "M", Additions: added}},
		Contents:         []harness.FileContent{{Path: "README.md", Mode: "100644", Data: []byte(after)}},
		ContentsComplete: true,
	}
}

// play runs one simulated session.
func (e *Executor) play(r *run) {
	defer e.wg.Done()
	speed := e.Speed
	if speed <= 0 {
		speed = 1
	}
	emit := func(ev harness.Event) {
		ev.Time = e.now().UTC()
		if r.hooks.OnEvent != nil {
			r.hooks.OnEvent(ev)
		}
	}
	state := func(s string) {
		if r.hooks.OnState != nil {
			r.hooks.OnState(s)
		}
	}
	cancelled := false
	wait := func(d time.Duration) bool {
		select {
		case <-r.cancel:
			cancelled = true
			return false
		case <-time.After(time.Duration(float64(d) / speed)):
			return true
		}
	}
	begin := e.now()
	kind := jobKind(r.spec)
	state(harness.StatePreparing)
	emit(harness.Event{Kind: harness.EventSystem, Text: "Creando el workspace ws-" + r.id + "…"})
	if e.ax != nil {
		e.ax.addRun(r.id, r.spec.Repo, r.spec.Branch, e.now())
	}
	started := false
	outcome, message := harness.OutcomeExited, ""
	exit := 0
	var final string
	var usage harness.Usage
	var ch *harness.Changes
	if wait(800 * time.Millisecond) {
		emit(harness.Event{Kind: harness.EventSystem, Text: "Sandbox listo"})
		emit(harness.Event{Kind: harness.EventSystem, Text: "Repositorio clonado en " + fmt.Sprintf("%07x", e.intn(1<<28))})
		if len(r.spec.ApplyPatch) > 0 {
			emit(harness.Event{Kind: harness.EventSystem, Text: "Cambios del paso anterior aplicados"})
		}
		if e.Credentials != nil {
			if _, err := e.Credentials(r.spec.Harness); err != nil {
				outcome, message = harness.OutcomeFailed, "La credencial del agente no está disponible"
			}
		}
	}
	if !cancelled && outcome == harness.OutcomeExited {
		state(harness.StateRunning)
		started = true
		evs := e.script(r.spec, kind)
		per := 15 * time.Second / time.Duration(len(evs)+1)
		for _, ev := range evs {
			if !wait(per) {
				break
			}
			emit(ev)
		}
	}
	if started && !cancelled {
		final = e.result(r.spec, kind)
		turns := 3 + e.intn(20)
		usage = harness.Usage{
			ModelUsed: r.spec.Model, CostUSD: float64(5+e.intn(80)) / 100, InTokens: int64(20000 + e.intn(80000)),
			OutTokens: int64(1000 + e.intn(9000)), CacheRead: int64(e.intn(50000)), Turns: turns,
			DurationMS: e.now().Sub(begin).Milliseconds(),
		}
		emit(harness.Event{Kind: harness.EventText, Text: final})
		emit(harness.Event{Kind: harness.EventResult, Text: harness.Clip(final, harness.MaxEventText), CostUSD: usage.CostUSD,
			InTokens: usage.InTokens, OutTokens: usage.OutTokens, CacheRead: usage.CacheRead, Turns: turns,
			Millis: usage.DurationMS})
		if r.spec.CaptureChanges {
			ch = changes(r.spec, 1+len(r.spec.ApplyPatch)%3)
			emit(harness.Event{Kind: harness.EventSystem, Text: "Cambios recogidos: 1 fichero"})
		}
		if r.spec.Harness == harness.Codex && e.SaveCodexAuth != nil && e.Credentials != nil {
			// Like the run manager, hand back auth.json after a Codex run;
			// the demo's is unchanged, so keeping it is a no-op.
			if env, err := e.Credentials(harness.Codex); err == nil {
				if data, err := base64.StdEncoding.DecodeString(env["CODEX_AUTH_JSON_B64"]); err == nil {
					if err := e.SaveCodexAuth(data, r.created); err != nil {
						emit(harness.Event{Kind: harness.EventSystem, Text: "Aviso: no se guardó la credencial renovada de Codex"})
					}
				}
			}
		}
	}
	if cancelled {
		outcome, message = harness.OutcomeCancelled, "Cancelada desde la Oficina"
		if started {
			emit(harness.Event{Kind: harness.EventSystem, Text: "Deteniendo el agente (SIGTERM)…"})
		}
	}
	state(harness.StateCleaning)
	emit(harness.Event{Kind: harness.EventSystem, Text: "Borrando la tarea " + r.id + " y su workspace…"})
	if e.ax != nil {
		e.ax.removeRun(r.id)
	}
	r.res = harness.Result{Outcome: outcome, Message: message, ResultText: final, Usage: usage, Changes: ch}
	if outcome == harness.OutcomeExited {
		r.res.ExitCode = &exit
	}
	state(harness.StateFinished)
	e.mu.Lock()
	if e.active == r {
		e.active = nil
	}
	e.mu.Unlock()
	close(r.done)
}
