// Package web serves the Oficina de agentes: the JSON API, one SSE stream
// and the static UI bundle, behind Traefik's basicAuth and mTLS. It also
// keeps the raw AX views (tasks, workspaces, gateways).
package web

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/office"
)

// Limits of the HTTP layer.
const (
	MaxBodyBytes = 128 << 10
	TaskPage     = 50
	maxOffset    = 100000
	// CSP: no inline code, nothing from other origins, no framing.
	CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; " +
		"img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
	// CSRFHeader must accompany every POST; a cross-site form cannot set it.
	CSRFHeader = "X-AX-Web"
	// DefaultStreamMax ends an event stream so the browser reconnects
	// well within Traefik's 3600 s readTimeout.
	DefaultStreamMax = 30 * time.Minute
	// StreamReplay is how many stored events a job stream starts with.
	StreamReplay = 1500
)

// Names of the tasks the panel and ax-tarea create.
const (
	taskPrefix      = "web-"
	hostRunPrefix   = "tarea-"
	workspacePrefix = "ws-"
)

var taskName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)

// TaskRunner is what the raw AX views need from the run manager.
type TaskRunner interface {
	// ActiveTask is the task of the run in progress, or "".
	ActiveTask() string
	// DeleteAndWait deletes a task and a workspace (either may be "")
	// until AX no longer has them.
	DeleteAndWait(task, workspace string, timeout time.Duration) error
}

// Server holds the handlers' dependencies.
type Server struct {
	AX       v1alpha1.AXClient
	Tasks    TaskRunner
	Office   *office.Office
	Atespace string
	// Origin and ExtraOrigins are the only origins a POST may come from.
	Origin       string
	ExtraOrigins []string
	Blackout     config.Window
	WatchdogLead time.Duration
	Certs        *CertWatch
	Now          func() time.Time
	Audit        *slog.Logger
	PingInterval time.Duration
	// StreamMax bounds one event stream (DefaultStreamMax when zero).
	StreamMax   time.Duration
	CallTimeout time.Duration
	// DeleteWait is how long a delete request waits for AX before it
	// answers 202; DeleteTimeout bounds the background wait after that.
	// Traefik gives the panel 60 s to send response headers.
	DeleteWait    time.Duration
	DeleteTimeout time.Duration
	// Stop ends open SSE streams when the panel shuts down.
	Stop <-chan struct{}
	// Static is the UI (StaticFS when nil).
	Static  fs.FS
	Version string

	deletes chan struct{}
	assets  *bundle
}

// maxDeletes bounds background deletions.
const maxDeletes = 4

// Handler is the whole application, with the security layers applied to
// every response.
func (s *Server) Handler() http.Handler {
	s.deletes = make(chan struct{}, maxDeletes)
	if s.Static == nil {
		s.Static = StaticFS()
	}
	s.assets = buildBundle(s.Static)
	if s.assets.err != nil && s.Audit != nil {
		s.Audit.Error("ui.bundle", "error", s.assets.err.Error())
	}
	if s.Certs == nil {
		s.Certs = &CertWatch{}
	}
	if s.Now == nil {
		s.Now = time.Now
	}
	if s.Audit == nil {
		s.Audit = slog.New(slog.DiscardHandler)
	}
	mux := http.NewServeMux()
	// For an end-to-end probe through Traefik and mTLS; it reveals nothing.
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "ok\n")
	})
	mux.HandleFunc("GET /{$}", s.index)
	mux.HandleFunc("GET /assets/{name}", s.asset)
	mux.HandleFunc("GET /favicon.svg", s.favicon)
	// Raw AX views.
	mux.HandleFunc("GET /api/status", s.status)
	mux.HandleFunc("GET /api/tasks", s.listTasks)
	mux.HandleFunc("GET /api/tasks/{name}", s.getTask)
	mux.HandleFunc("POST /api/tasks/{name}/suspend", s.suspend)
	mux.HandleFunc("POST /api/tasks/{name}/resume", s.resume)
	mux.HandleFunc("POST /api/tasks/{name}/delete", s.deleteTask)
	mux.HandleFunc("GET /api/gateways", s.gateways)
	mux.HandleFunc("GET /api/workspaces", s.workspaces)
	s.officeRoutes(mux)
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
	if origin != "" && origin != s.Origin && !slices.Contains(s.ExtraOrigins, origin) {
		return false
	}
	if site != "" && site != "same-origin" {
		return false
	}
	return origin != "" || site != ""
}

// writeJSON answers v as JSON. It is encoded before anything is sent: a
// value that cannot be (a NaN, say) is a 500 with an error, never an
// empty 200.
func writeJSON(w http.ResponseWriter, code int, v any) {
	data, err := json.Marshal(v)
	if err != nil {
		code, data = http.StatusInternalServerError, []byte(`{"error":"no se pudo serializar la respuesta"}`)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_, _ = w.Write(append(data, '\n'))
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

var errBody = errors.New("el cuerpo supera 128 KiB")

// decode reads exactly one JSON object with only known fields. An empty
// body counts as {} when empty is true.
func decode(r *http.Request, v any, empty bool) error {
	data, err := io.ReadAll(r.Body)
	if err != nil {
		return errBody
	}
	if empty && len(bytes.TrimSpace(data)) == 0 {
		return nil
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

func (s *Server) activeTask() string {
	if s.Tasks == nil {
		return ""
	}
	return s.Tasks.ActiveTask()
}

func (s *Server) status(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"version":     s.Version,
		"active":      s.activeTask(),
		"blackout":    s.Blackout.String(),
		"in_blackout": s.inBlackout(),
		"warnings":    nonNil(s.Certs.Warnings(s.Now())),
	})
}

func nonNil(v []string) []string {
	if v == nil {
		return []string{}
	}
	return v
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
	active := s.activeTask()
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
	v.PanelRun = v.Name == s.activeTask()
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
	case len(t.GetSpec().GetEnv()) > 0 || strings.HasPrefix(name, hostRunPrefix) ||
		strings.HasPrefix(name, taskPrefix):
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
	if !ok {
		return
	}
	if err := decode(r, &struct{}{}, true); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	ctx, cancel := s.call(r)
	defer cancel()
	current, err := s.AX.GetTask(ctx, &v1alpha1.GetTaskRequest{Atespace: s.Atespace, Name: n})
	if err != nil {
		axError(w, err)
		return
	}
	if why := suspendRefusal(current, n == s.activeTask()); why != "" {
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
	if !ok {
		return
	}
	if err := decode(r, &struct{}{}, true); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
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
// DeleteWait and 202 otherwise, and the page checks the task again. A
// run's or ax-tarea's paired ws-<name> workspace goes with it.
func (s *Server) deleteTask(w http.ResponseWriter, r *http.Request) {
	n, ok := s.name(w, r)
	if !ok {
		return
	}
	var body struct {
		Confirm string `json:"confirm"`
	}
	if err := decode(r, &body, false); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if body.Confirm != n {
		writeError(w, http.StatusBadRequest, "escribe el nombre exacto de la tarea para confirmar")
		return
	}
	if n == s.activeTask() {
		writeError(w, http.StatusConflict, "es la ejecución en curso: cancela su trabajo en la Oficina")
		return
	}
	if s.Tasks == nil {
		writeError(w, http.StatusServiceUnavailable, "el gestor de ejecuciones no está disponible")
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
	if strings.HasPrefix(n, taskPrefix) || strings.HasPrefix(n, hostRunPrefix) {
		workspace = workspacePrefix + n
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
		err := s.Tasks.DeleteAndWait(n, workspace, s.DeleteTimeout)
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
