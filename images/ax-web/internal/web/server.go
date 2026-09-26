// Package web serves the panel: a few JSON endpoints, one SSE stream and
// three embedded static files, behind Traefik's basicAuth and mTLS.
package web

import (
	"bytes"
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/runs"
)

//go:embed static
var static embed.FS

// Limits of the HTTP layer.
const (
	MaxBodyBytes = 32 << 10
	TaskPage     = 50
	maxOffset    = 100000
	// CSP: no inline code, nothing from other origins, no framing.
	CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; " +
		"img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
	// CSRFHeader must accompany every POST; a cross-site form cannot set it.
	CSRFHeader = "X-AX-Web"
)

var taskName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)

// Server holds the handlers' dependencies.
type Server struct {
	AX           v1alpha1.AXClient
	Runs         *runs.Manager
	Atespace     string
	Origin       string
	Limits       runs.Limits
	Blackout     config.Window
	WatchdogLead time.Duration
	Certs        *CertWatch
	Now          func() time.Time
	Audit        *slog.Logger
	PingInterval time.Duration
	CallTimeout  time.Duration
	// DeleteWait is how long a delete request waits for AX before it
	// answers 202; DeleteTimeout bounds the background wait after that.
	// Traefik gives the panel 60 s to send response headers.
	DeleteWait    time.Duration
	DeleteTimeout time.Duration
	// Stop ends open SSE streams when the panel shuts down.
	Stop <-chan struct{}

	deletes chan struct{}
}

// maxDeletes bounds background deletions.
const maxDeletes = 4

// Handler is the whole application, with the security layers applied to
// every response.
func (s *Server) Handler() http.Handler {
	s.deletes = make(chan struct{}, maxDeletes)
	mux := http.NewServeMux()
	// For an end-to-end probe through Traefik and mTLS; it reveals nothing.
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "ok\n")
	})
	mux.HandleFunc("GET /{$}", s.file("static/index.html", "text/html; charset=utf-8"))
	mux.HandleFunc("GET /app.js", s.file("static/app.js", "text/javascript; charset=utf-8"))
	mux.HandleFunc("GET /app.css", s.file("static/app.css", "text/css; charset=utf-8"))
	mux.HandleFunc("GET /api/status", s.status)
	mux.HandleFunc("GET /api/tasks", s.listTasks)
	mux.HandleFunc("GET /api/tasks/{name}", s.getTask)
	mux.HandleFunc("POST /api/tasks/{name}/suspend", s.suspend)
	mux.HandleFunc("POST /api/tasks/{name}/resume", s.resume)
	mux.HandleFunc("POST /api/tasks/{name}/delete", s.deleteTask)
	mux.HandleFunc("GET /api/gateways", s.gateways)
	mux.HandleFunc("GET /api/workspaces", s.workspaces)
	mux.HandleFunc("POST /api/runs", s.startRun)
	mux.HandleFunc("GET /api/runs/current", s.currentRun)
	mux.HandleFunc("GET /api/runs/{id}/events", s.events)
	mux.HandleFunc("POST /api/runs/{id}/cancel", s.cancelRun)
	return s.secure(s.csrf(mux))
}

func (s *Server) secure(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("Content-Security-Policy", CSP)
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("Cache-Control", "no-store")
		h.Set("Cross-Origin-Opener-Policy", "same-origin")
		h.Set("Cross-Origin-Resource-Policy", "same-origin")
		h.Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		next.ServeHTTP(w, r)
	})
}

// csrf lets a state-changing request through only when it is a same-origin
// JSON POST carrying the panel's header. Browsers attach cached Basic
// credentials to cross-site requests, so Traefik's auth alone does not stop
// a forged one. No CORS header is ever sent.
func (s *Server) csrf(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet, http.MethodHead:
			next.ServeHTTP(w, r)
			return
		case http.MethodPost:
		default:
			writeError(w, http.StatusMethodNotAllowed, "método no permitido")
			return
		}
		if !s.sameOrigin(r) {
			writeError(w, http.StatusForbidden, "petición de otro origen rechazada")
			return
		}
		if r.Header.Get(CSRFHeader) != "1" {
			writeError(w, http.StatusForbidden, "falta la cabecera del panel")
			return
		}
		mt, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || mt != "application/json" {
			writeError(w, http.StatusUnsupportedMediaType, "solo se admite JSON")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, MaxBodyBytes)
		next.ServeHTTP(w, r)
	})
}

func (s *Server) sameOrigin(r *http.Request) bool {
	origin, site := r.Header.Get("Origin"), r.Header.Get("Sec-Fetch-Site")
	if origin != "" && origin != s.Origin {
		return false
	}
	if site != "" && site != "same-origin" {
		return false
	}
	return origin != "" || site != ""
}

func (s *Server) file(path, contentType string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		data, err := static.ReadFile(path)
		if err != nil {
			http.NotFound(w, nil)
			return
		}
		w.Header().Set("Content-Type", contentType)
		_, _ = w.Write(data)
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

// decode reads exactly one JSON object with only known fields.
func decode(r *http.Request, v any) error {
	data, err := io.ReadAll(r.Body)
	if err != nil {
		return errors.New("el cuerpo supera 32 KiB")
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return errors.New("JSON no válido")
	}
	if dec.More() {
		return errors.New("JSON no válido")
	}
	return nil
}

func (s *Server) call(r *http.Request) (context.Context, context.CancelFunc) {
	return context.WithTimeout(r.Context(), s.CallTimeout)
}

// axError maps an AX failure without echoing internals.
func axError(w http.ResponseWriter, err error) {
	if status.Code(err) == codes.NotFound {
		writeError(w, http.StatusNotFound, "no existe")
		return
	}
	writeError(w, http.StatusBadGateway, "AX no respondió correctamente")
}

// ClientIP is the address Traefik appended to X-Forwarded-For. Only Traefik
// can reach the panel (mTLS), so its last entry is trustworthy.
func ClientIP(r *http.Request) string {
	xff := r.Header.Values("X-Forwarded-For")
	if len(xff) > 0 {
		parts := strings.Split(xff[len(xff)-1], ",")
		if ip := net.ParseIP(strings.TrimSpace(parts[len(parts)-1])); ip != nil {
			return ip.String()
		}
	}
	return "desconocida"
}

func (s *Server) inBlackout() bool {
	now := s.Now()
	return s.Blackout.Overlaps(now, now.Add(s.WatchdogLead))
}

func (s *Server) status(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"manager":     s.Runs.Status(),
		"blackout":    s.Blackout.String(),
		"in_blackout": s.inBlackout(),
		"warnings":    s.Certs.Warnings(s.Now()),
		"limits": map[string]any{
			"max_turns": s.Limits.MaxTurns, "max_timeout_minutes": int(s.Limits.MaxTimeout / time.Minute),
			"repo_hosts": s.Limits.RepoHosts, "cpu": runs.CPUChoices, "memory": runs.MemoryChoices,
		},
	})
}

func (s *Server) listTasks(w http.ResponseWriter, r *http.Request) {
	offset := 0
	if q := r.URL.Query().Get("offset"); q != "" {
		n, err := strconv.Atoi(q)
		if err != nil || n < 0 || n > maxOffset {
			writeError(w, http.StatusBadRequest, "offset no válido")
			return
		}
		offset = n
	}
	ctx, cancel := s.call(r)
	defer cancel()
	resp, err := s.AX.ListTasks(ctx, &v1alpha1.ListTasksRequest{
		Atespace: s.Atespace, Limit: TaskPage, Offset: int64(offset),
	})
	if err != nil {
		axError(w, err)
		return
	}
	active := s.Runs.ActiveTask()
	out := []TaskView{}
	for _, t := range resp.GetTasks() {
		v := NewTaskView(t)
		v.PanelRun = v.Name == active
		out = append(out, v)
	}
	body := map[string]any{"tasks": out, "offset": offset}
	if len(out) == TaskPage {
		body["next"] = offset + TaskPage
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) name(w http.ResponseWriter, r *http.Request) (string, bool) {
	n := r.PathValue("name")
	if !taskName.MatchString(n) {
		writeError(w, http.StatusBadRequest, "nombre de tarea no válido")
		return "", false
	}
	return n, true
}

func (s *Server) taskView(t *v1alpha1.Task) TaskView {
	v := NewTaskView(t)
	v.PanelRun = v.Name == s.Runs.ActiveTask()
	v.CanSuspend = !v.Suspend && suspendRefusal(t, v.PanelRun) == ""
	v.CanResume = v.Suspend && !s.inBlackout()
	return v
}

// suspendRefusal says why a checkpoint of t must not be taken: it would
// persist a credential that the task holds in its env (ax-tarea's runs) or
// that a panel run's agent holds in memory.
func suspendRefusal(t *v1alpha1.Task, panelRun bool) string {
	name := t.GetMetadata().GetName()
	switch {
	case panelRun:
		return "Suspender está desactivado mientras el panel ejecuta el agente en esta tarea"
	case len(t.GetSpec().GetEnv()) > 0 || strings.HasPrefix(name, runs.HostRunPrefix) ||
		strings.HasPrefix(name, runs.TaskPrefix):
		return "Suspender está desactivado en tareas que llevan la credencial de un agente"
	}
	return ""
}

func (s *Server) getTask(w http.ResponseWriter, r *http.Request) {
	n, ok := s.name(w, r)
	if !ok {
		return
	}
	ctx, cancel := s.call(r)
	defer cancel()
	t, err := s.AX.GetTask(ctx, &v1alpha1.GetTaskRequest{Atespace: s.Atespace, Name: n})
	if err != nil {
		axError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.taskView(t))
}

func (s *Server) suspend(w http.ResponseWriter, r *http.Request) {
	n, ok := s.name(w, r)
	if !ok || decode(r, &struct{}{}) != nil {
		if ok {
			writeError(w, http.StatusBadRequest, "JSON no válido")
		}
		return
	}
	ctx, cancel := s.call(r)
	defer cancel()
	current, err := s.AX.GetTask(ctx, &v1alpha1.GetTaskRequest{Atespace: s.Atespace, Name: n})
	if err != nil {
		axError(w, err)
		return
	}
	if why := suspendRefusal(current, n == s.Runs.ActiveTask()); why != "" {
		writeError(w, http.StatusConflict, why)
		return
	}
	t, err := s.AX.SuspendTask(ctx, &v1alpha1.SuspendTaskRequest{Atespace: s.Atespace, Name: n})
	s.Audit.Info("audit", "action", "task.suspend", "task", n, "client_ip", ClientIP(r), "ok", err == nil)
	if err != nil {
		axError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.taskView(t))
}

func (s *Server) resume(w http.ResponseWriter, r *http.Request) {
	n, ok := s.name(w, r)
	if !ok || decode(r, &struct{}{}) != nil {
		if ok {
			writeError(w, http.StatusBadRequest, "JSON no válido")
		}
		return
	}
	if s.inBlackout() {
		writeError(w, http.StatusConflict,
			"Reanudar no está permitido durante la ventana del Observatorio ("+s.Blackout.String()+" UTC)")
		return
	}
	ctx, cancel := s.call(r)
	defer cancel()
	t, err := s.AX.ResumeTask(ctx, &v1alpha1.ResumeTaskRequest{Atespace: s.Atespace, Name: n})
	s.Audit.Info("audit", "action", "task.resume", "task", n, "client_ip", ClientIP(r), "ok", err == nil)
	if err != nil {
		axError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.taskView(t))
}

// deleteTask removes a task only when the body repeats its exact name and,
// like ax-tarea, keeps deleting until AX no longer has it. That wait runs
// in the background: the answer is 200 if the task is gone within
// DeleteWait and 202 otherwise, and the page polls the task. A run's or
// ax-tarea's paired ws-<name> workspace goes with it.
func (s *Server) deleteTask(w http.ResponseWriter, r *http.Request) {
	n, ok := s.name(w, r)
	if !ok {
		return
	}
	var body struct {
		Confirm string `json:"confirm"`
	}
	if err := decode(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if body.Confirm != n {
		writeError(w, http.StatusBadRequest, "escribe el nombre exacto de la tarea para confirmar")
		return
	}
	if n == s.Runs.ActiveTask() {
		writeError(w, http.StatusConflict, "es la ejecución en curso: usa Cancelar")
		return
	}
	ctx, cancel := s.call(r)
	_, err := s.AX.GetTask(ctx, &v1alpha1.GetTaskRequest{Atespace: s.Atespace, Name: n})
	cancel()
	if err != nil {
		axError(w, err)
		return
	}
	workspace := ""
	if strings.HasPrefix(n, runs.TaskPrefix) || strings.HasPrefix(n, runs.HostRunPrefix) {
		workspace = runs.WorkspacePrefix + n
	}
	select {
	case s.deletes <- struct{}{}:
	default:
		writeError(w, http.StatusTooManyRequests, "hay demasiados borrados en curso")
		return
	}
	client := ClientIP(r)
	done := make(chan error, 1)
	go func() {
		defer func() { <-s.deletes }()
		err := s.Runs.DeleteAndWait(n, workspace, s.DeleteTimeout)
		s.Audit.Info("audit", "action", "task.delete", "task", n, "workspace", workspace,
			"client_ip", client, "gone", err == nil)
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil {
			writeJSON(w, http.StatusOK, map[string]any{"deleted": true})
			return
		}
	case <-time.After(s.DeleteWait):
	}
	writeJSON(w, http.StatusAccepted, map[string]any{
		"deleted": false,
		"message": "AX aún está borrándola; la página comprobará cuándo desaparece.",
	})
}

func (s *Server) gateways(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := s.call(r)
	defer cancel()
	resp, err := s.AX.ListGateways(ctx, &v1alpha1.ListGatewaysRequest{Atespace: s.Atespace})
	if err != nil {
		axError(w, err)
		return
	}
	out := []GatewayView{}
	for _, g := range resp.GetGateways() {
		out = append(out, NewGatewayView(g))
	}
	writeJSON(w, http.StatusOK, map[string]any{"gateways": out})
}

func (s *Server) workspaces(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := s.call(r)
	defer cancel()
	resp, err := s.AX.ListWorkspaces(ctx, &v1alpha1.ListWorkspacesRequest{Atespace: s.Atespace})
	if err != nil {
		axError(w, err)
		return
	}
	out := []WorkspaceView{}
	for _, ws := range resp.GetWorkspaces() {
		out = append(out, NewWorkspaceView(ws))
	}
	writeJSON(w, http.StatusOK, map[string]any{"workspaces": out})
}

func (s *Server) startRun(w http.ResponseWriter, r *http.Request) {
	var req runs.Request
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	spec, err := runs.Validate(req, s.Limits)
	if err != nil {
		var fe *runs.FieldError
		if errors.As(err, &fe) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": fe.Message, "field": fe.Field})
			return
		}
		writeError(w, http.StatusBadRequest, "petición no válida")
		return
	}
	run, err := s.Runs.Start(r.Context(), spec, ClientIP(r))
	var busy *runs.BusyError
	switch {
	case err == nil:
		writeJSON(w, http.StatusAccepted, run.View())
	case errors.As(err, &busy):
		writeError(w, http.StatusConflict, busy.Error())
	case errors.Is(err, runs.ErrBlackout):
		writeError(w, http.StatusConflict, fmt.Sprintf("%s (%s UTC)", err, s.Blackout))
	case errors.Is(err, runs.ErrNotReady), errors.Is(err, runs.ErrShuttingDown):
		writeError(w, http.StatusServiceUnavailable, err.Error())
	default:
		writeError(w, http.StatusBadGateway, "AX no respondió correctamente")
	}
}

func (s *Server) currentRun(w http.ResponseWriter, _ *http.Request) {
	if run := s.Runs.Current(); run != nil {
		writeJSON(w, http.StatusOK, map[string]any{"run": run.View()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run": nil})
}

func (s *Server) cancelRun(w http.ResponseWriter, r *http.Request) {
	if decode(r, &struct{}{}) != nil {
		writeError(w, http.StatusBadRequest, "JSON no válido")
		return
	}
	id := r.PathValue("id")
	if err := s.Runs.Cancel(id, "cancelada desde el panel por "+ClientIP(r)); err != nil {
		writeError(w, http.StatusNotFound, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]any{"cancelling": true})
}

type outEvent struct {
	S string `json:"s"`
	D string `json:"d"`
}

// events streams the run's log as Server-Sent Events. The headers and a
// comment go out at once (Traefik's responseHeaderTimeout is 60 s), a
// comment every PingInterval keeps idle proxies from closing it, and each
// event's id is the log offset after it, so a reconnecting EventSource
// resumes from Last-Event-ID.
func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	run := s.Runs.Get(r.PathValue("id"))
	if run == nil {
		writeError(w, http.StatusNotFound, runs.ErrNotFound.Error())
		return
	}
	from := int64(0)
	last := r.Header.Get("Last-Event-ID")
	if last != "" {
		n, err := strconv.ParseInt(last, 10, 64)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "Last-Event-ID no válido")
			return
		}
		from = n
	}
	// A browser that already saw the end of a finished run gets 204, which
	// stops EventSource from reconnecting.
	if last != "" {
		if chunks, _, closed, _ := run.Output.Read(from); closed && len(chunks) == 0 {
			w.WriteHeader(http.StatusNoContent)
			return
		}
	}
	rc := http.NewResponseController(w)
	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	if _, err := io.WriteString(w, "retry: 5000\n: ok\n\n"); err != nil {
		return
	}
	if rc.Flush() != nil {
		return
	}
	ping := time.NewTicker(s.PingInterval)
	defer ping.Stop()
	state := ""
	for {
		chunks, truncated, closed, wait := run.Output.Read(from)
		var buf bytes.Buffer
		if truncated {
			buf.WriteString("event: truncated\ndata: {}\n\n")
		}
		for _, c := range chunks {
			data, _ := json.Marshal(outEvent{S: c.Stream, D: c.Text})
			fmt.Fprintf(&buf, "id: %d\nevent: out\ndata: %s\n\n", c.End, data)
			from = c.End
		}
		// Every state change is logged, so it is noticed here; the page
		// needs no polling.
		if v := run.View(); v.State != state && !closed {
			state = v.State
			data, _ := json.Marshal(v)
			fmt.Fprintf(&buf, "event: state\ndata: %s\n\n", data)
		}
		if closed {
			data, _ := json.Marshal(run.View())
			fmt.Fprintf(&buf, "event: end\ndata: %s\n\n", data)
		}
		if buf.Len() > 0 {
			if _, err := w.Write(buf.Bytes()); err != nil || rc.Flush() != nil {
				return
			}
		}
		if closed {
			return
		}
		select {
		case <-wait:
		case <-ping.C:
			if _, err := io.WriteString(w, ": ping\n\n"); err != nil || rc.Flush() != nil {
				return
			}
		case <-r.Context().Done():
			return
		case <-s.Stop:
			return
		}
	}
}
