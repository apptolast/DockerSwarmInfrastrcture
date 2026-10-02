package web

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"apptolast.com/ax-web/internal/office"
)

var (
	pipelineID = regexp.MustCompile(`^p-[0-9]{1,12}$`)
	proposalID = regexp.MustCompile(`^pr-[0-9]{1,12}$`)
)

// githubCallTimeout bounds a request that calls GitHub: Traefik waits
// 60 s for the response headers.
const githubCallTimeout = 50 * time.Second

func (s *Server) officeRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/office", s.snapshot)
	mux.HandleFunc("GET /api/stream", s.stream)
	mux.HandleFunc("GET /api/export", s.export)

	mux.HandleFunc("POST /api/agents", s.createAgent)
	mux.HandleFunc("POST /api/agents/{id}", s.withID(office.ValidID, s.updateAgent))
	mux.HandleFunc("POST /api/agents/{id}/delete", s.withID(office.ValidID, s.deleteAgent))
	mux.HandleFunc("POST /api/agents/{id}/coach", s.withID(office.ValidID, s.coachAgent))
	mux.HandleFunc("POST /api/agents/{id}/rollback", s.withID(office.ValidID, s.rollbackAgent))

	mux.HandleFunc("POST /api/projects", s.createProject)
	mux.HandleFunc("POST /api/projects/{id}", s.withID(office.ValidID, s.updateProject))
	mux.HandleFunc("POST /api/projects/{id}/delete", s.withID(office.ValidID, s.deleteProject))
	mux.HandleFunc("POST /api/projects/{id}/memory", s.withID(office.ValidID, s.projectMemory))
	mux.HandleFunc("GET /api/projects/{id}/github", s.withID(office.ValidID, s.projectGitHub))

	mux.HandleFunc("POST /api/jobs", s.createJob)
	mux.HandleFunc("GET /api/jobs/{id}", s.withID(office.ValidJobID, s.getJob))
	mux.HandleFunc("GET /api/jobs/{id}/events", s.withID(office.ValidJobID, s.jobEvents))
	mux.HandleFunc("GET /api/jobs/{id}/patch", s.withID(office.ValidJobID, s.jobPatch))
	mux.HandleFunc("POST /api/jobs/{id}/cancel", s.withID(office.ValidJobID, s.cancelJob))
	mux.HandleFunc("POST /api/jobs/{id}/retry", s.withID(office.ValidJobID, s.retryJob))
	mux.HandleFunc("POST /api/jobs/{id}/delete", s.withID(office.ValidJobID, s.deleteJob))
	mux.HandleFunc("POST /api/jobs/{id}/rate", s.withID(office.ValidJobID, s.rateJob))
	mux.HandleFunc("POST /api/jobs/{id}/priority", s.withID(office.ValidJobID, s.jobPriority))
	mux.HandleFunc("GET /api/jobs/{id}/github-preview", s.withID(office.ValidJobID, s.jobGitHubPreview))
	mux.HandleFunc("POST /api/jobs/{id}/pr", s.withID(office.ValidJobID, s.jobPR))
	mux.HandleFunc("POST /api/jobs/{id}/comment", s.withID(office.ValidJobID, s.jobComment))
	mux.HandleFunc("POST /api/jobs/{id}/followup", s.withID(office.ValidJobID, s.jobFollowUp))

	mux.HandleFunc("POST /api/pipelines", s.createPipeline)
	mux.HandleFunc("GET /api/pipelines/{id}", s.withID(pipelineID.MatchString, s.getPipeline))
	mux.HandleFunc("POST /api/pipelines/{id}/cancel", s.withID(pipelineID.MatchString, s.cancelPipeline))

	mux.HandleFunc("POST /api/schedules", s.saveSchedule)
	mux.HandleFunc("POST /api/schedules/{id}/delete", s.withID(office.ValidID, s.deleteSchedule))
	mux.HandleFunc("POST /api/schedules/{id}/run", s.withID(office.ValidID, s.runSchedule))

	mux.HandleFunc("POST /api/proposals/{id}/approve", s.withID(proposalID.MatchString, s.approveProposal))
	mux.HandleFunc("POST /api/proposals/{id}/reject", s.withID(proposalID.MatchString, s.rejectProposal))

	mux.HandleFunc("POST /api/evals", s.saveEval)
	mux.HandleFunc("POST /api/evals/run", s.runEvals)
	mux.HandleFunc("POST /api/evals/{id}/delete", s.withID(office.ValidID, s.deleteEval))

	mux.HandleFunc("POST /api/settings", s.updateSettings)
	mux.HandleFunc("POST /api/queue/pause", s.pauseQueue)
	mux.HandleFunc("POST /api/credentials/codex/forget", s.forgetCodex)
	mux.HandleFunc("GET /api/github/repos", s.githubRepos)
}

type idHandler func(w http.ResponseWriter, r *http.Request, id string)

// withID checks the {id} path value before the handler sees it.
func (s *Server) withID(valid func(string) bool, h idHandler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		if !valid(id) {
			writeError(w, http.StatusBadRequest, "identificador no válido")
			return
		}
		h(w, r, id)
	}
}

// fail answers an office error: a field error names the field, an
// office.Error keeps its status, anything else is a 500 without details.
func (s *Server) fail(w http.ResponseWriter, err error) {
	var fe *office.FieldError
	if errors.As(err, &fe) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": fe.Message, "field": fe.Field})
		return
	}
	var oe *office.Error
	if errors.As(err, &oe) {
		writeError(w, oe.Status, oe.Message)
		return
	}
	s.Audit.Error("office.error", "error", err.Error())
	writeError(w, http.StatusInternalServerError, "error interno de la Oficina")
}

// body decodes a POST body or answers 400.
func body(w http.ResponseWriter, r *http.Request, v any, emptyOK bool) bool {
	if err := decode(r, v, emptyOK); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return false
	}
	return true
}

func (s *Server) reply(w http.ResponseWriter, code int, v any, err error) {
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, code, v)
}

func (s *Server) snapshot(w http.ResponseWriter, _ *http.Request) {
	snap := s.Office.Snapshot()
	snap.Warnings = append(snap.Warnings, s.Certs.Warnings(s.Now())...)
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) export(w http.ResponseWriter, _ *http.Request) {
	data, err := s.Office.Export()
	if err != nil {
		s.fail(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="oficina-%s.json"`,
		s.Now().UTC().Format("2006-01-02")))
	_, _ = w.Write(data)
}

// Agents.

func (s *Server) createAgent(w http.ResponseWriter, r *http.Request) {
	var in office.AgentInput
	if body(w, r, &in, false) {
		a, err := s.Office.CreateAgent(in, ClientIP(r))
		s.reply(w, http.StatusCreated, a, err)
	}
}

func (s *Server) updateAgent(w http.ResponseWriter, r *http.Request, id string) {
	var in office.AgentInput
	if body(w, r, &in, false) {
		a, err := s.Office.UpdateAgent(id, in, ClientIP(r))
		s.reply(w, http.StatusOK, a, err)
	}
}

func (s *Server) deleteAgent(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusOK, map[string]bool{"deleted": true}, s.Office.DeleteAgent(id, ClientIP(r)))
	}
}

func (s *Server) coachAgent(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		j, err := s.Office.CoachAgent(id, ClientIP(r))
		s.reply(w, http.StatusCreated, j, err)
	}
}

func (s *Server) rollbackAgent(w http.ResponseWriter, r *http.Request, id string) {
	var in struct {
		Version int `json:"version"`
	}
	if body(w, r, &in, false) {
		a, err := s.Office.RollbackAgent(id, in.Version, ClientIP(r))
		s.reply(w, http.StatusOK, a, err)
	}
}

// Projects.

func (s *Server) createProject(w http.ResponseWriter, r *http.Request) {
	var in office.ProjectInput
	if body(w, r, &in, false) {
		p, err := s.Office.CreateProject(in, ClientIP(r))
		s.reply(w, http.StatusCreated, p, err)
	}
}

func (s *Server) updateProject(w http.ResponseWriter, r *http.Request, id string) {
	var in office.ProjectInput
	if body(w, r, &in, false) {
		p, err := s.Office.UpdateProject(id, in, ClientIP(r))
		s.reply(w, http.StatusOK, p, err)
	}
}

func (s *Server) deleteProject(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusOK, map[string]bool{"deleted": true}, s.Office.DeleteProject(id, ClientIP(r)))
	}
}

func (s *Server) projectMemory(w http.ResponseWriter, r *http.Request, id string) {
	var in office.MemoryRequest
	if body(w, r, &in, false) {
		p, err := s.Office.MemoryAction(id, in, ClientIP(r))
		s.reply(w, http.StatusOK, p, err)
	}
}

func (s *Server) projectGitHub(w http.ResponseWriter, r *http.Request, id string) {
	ctx, cancel := context.WithTimeout(r.Context(), githubCallTimeout)
	defer cancel()
	v, err := s.Office.GitHub(ctx, id)
	s.reply(w, http.StatusOK, v, err)
}

// Jobs.

func (s *Server) createJob(w http.ResponseWriter, r *http.Request) {
	var in office.JobRequest
	if body(w, r, &in, false) {
		ctx, cancel := context.WithTimeout(r.Context(), githubCallTimeout)
		defer cancel()
		j, err := s.Office.CreateJob(ctx, in, ClientIP(r))
		s.reply(w, http.StatusCreated, j, err)
	}
}

func (s *Server) getJob(w http.ResponseWriter, _ *http.Request, id string) {
	d, err := s.Office.JobDetail(id)
	s.reply(w, http.StatusOK, d, err)
}

func (s *Server) jobEvents(w http.ResponseWriter, r *http.Request, id string) {
	var after int64
	if q := r.URL.Query().Get("after"); q != "" {
		n, err := strconv.ParseInt(q, 10, 64)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "after no válido")
			return
		}
		after = n
	}
	evs, more, err := s.Office.Events(id, after, 1000)
	s.reply(w, http.StatusOK, map[string]any{"events": evs, "more": more}, err)
}

func (s *Server) jobPatch(w http.ResponseWriter, r *http.Request, id string) {
	data, err := s.Office.Patch(id)
	if err != nil {
		s.fail(w, err)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	if r.URL.Query().Get("download") == "1" {
		w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="%s.patch"`, id))
	}
	_, _ = w.Write(data)
}

func (s *Server) cancelJob(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusAccepted, map[string]bool{"cancelling": true}, s.Office.CancelJob(id, ClientIP(r)))
	}
}

func (s *Server) retryJob(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		j, err := s.Office.RetryJob(id, ClientIP(r))
		s.reply(w, http.StatusCreated, j, err)
	}
}

func (s *Server) deleteJob(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusOK, map[string]bool{"deleted": true}, s.Office.DeleteJob(id, ClientIP(r)))
	}
}

func (s *Server) rateJob(w http.ResponseWriter, r *http.Request, id string) {
	var in struct {
		Score int    `json:"score"`
		Note  string `json:"note"`
	}
	if body(w, r, &in, false) {
		j, err := s.Office.RateJob(id, in.Score, in.Note, ClientIP(r))
		s.reply(w, http.StatusOK, j, err)
	}
}

func (s *Server) jobPriority(w http.ResponseWriter, r *http.Request, id string) {
	var in struct {
		Priority int `json:"priority"`
	}
	if body(w, r, &in, false) {
		j, err := s.Office.SetPriority(id, in.Priority, ClientIP(r))
		s.reply(w, http.StatusOK, j, err)
	}
}

// jobGitHubPreview shows exactly what a pull request (?target=pr) or a
// comment (?target=comment) of the job would publish.
func (s *Server) jobGitHubPreview(w http.ResponseWriter, r *http.Request, id string) {
	pv, err := s.Office.GitHubPreview(id, r.URL.Query().Get("target"))
	s.reply(w, http.StatusOK, pv, err)
}

func (s *Server) jobPR(w http.ResponseWriter, r *http.Request, id string) {
	var in office.PRInput
	if body(w, r, &in, true) {
		ctx, cancel := context.WithTimeout(r.Context(), githubCallTimeout)
		defer cancel()
		j, err := s.Office.CreatePR(ctx, id, in, ClientIP(r))
		s.reply(w, http.StatusOK, j, err)
	}
}

func (s *Server) jobComment(w http.ResponseWriter, r *http.Request, id string) {
	var in office.CommentInput
	if body(w, r, &in, false) {
		ctx, cancel := context.WithTimeout(r.Context(), githubCallTimeout)
		defer cancel()
		url, err := s.Office.CommentJob(ctx, id, in, ClientIP(r))
		s.reply(w, http.StatusOK, map[string]string{"url": url}, err)
	}
}

func (s *Server) jobFollowUp(w http.ResponseWriter, r *http.Request, id string) {
	var in office.FollowUpRequest
	if body(w, r, &in, false) {
		j, err := s.Office.FollowUp(id, in, ClientIP(r))
		s.reply(w, http.StatusCreated, j, err)
	}
}

// Pipelines.

func (s *Server) createPipeline(w http.ResponseWriter, r *http.Request) {
	var in office.PipelineRequest
	if body(w, r, &in, false) {
		ctx, cancel := context.WithTimeout(r.Context(), githubCallTimeout)
		defer cancel()
		p, err := s.Office.CreatePipeline(ctx, in, ClientIP(r))
		s.reply(w, http.StatusCreated, p, err)
	}
}

func (s *Server) getPipeline(w http.ResponseWriter, _ *http.Request, id string) {
	p, jobs, err := s.Office.Pipeline(id)
	s.reply(w, http.StatusOK, map[string]any{"pipeline": p, "jobs": jobs}, err)
}

func (s *Server) cancelPipeline(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusAccepted, map[string]bool{"cancelling": true}, s.Office.CancelPipeline(id, ClientIP(r)))
	}
}

// Schedules.

func (s *Server) saveSchedule(w http.ResponseWriter, r *http.Request) {
	var in office.ScheduleInput
	if body(w, r, &in, false) {
		if in.ID != "" && !office.ValidID(in.ID) {
			writeError(w, http.StatusBadRequest, "identificador no válido")
			return
		}
		sc, err := s.Office.SaveSchedule(in, ClientIP(r))
		code := http.StatusCreated
		if in.ID != "" {
			code = http.StatusOK
		}
		s.reply(w, code, sc, err)
	}
}

func (s *Server) deleteSchedule(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusOK, map[string]bool{"deleted": true}, s.Office.DeleteSchedule(id, ClientIP(r)))
	}
}

func (s *Server) runSchedule(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		sc, err := s.Office.RunSchedule(r.Context(), id, ClientIP(r))
		s.reply(w, http.StatusOK, sc, err)
	}
}

// Proposals.

func (s *Server) approveProposal(w http.ResponseWriter, r *http.Request, id string) {
	var in struct {
		Content *string `json:"content"`
	}
	if body(w, r, &in, true) {
		p, err := s.Office.ApproveProposal(id, in.Content, ClientIP(r))
		s.reply(w, http.StatusOK, p, err)
	}
}

func (s *Server) rejectProposal(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		p, err := s.Office.RejectProposal(id, ClientIP(r))
		s.reply(w, http.StatusOK, p, err)
	}
}

// Evaluations.

func (s *Server) saveEval(w http.ResponseWriter, r *http.Request) {
	var in office.EvalInput
	if body(w, r, &in, false) {
		if in.ID != "" && !office.ValidID(in.ID) {
			writeError(w, http.StatusBadRequest, "identificador no válido")
			return
		}
		e, err := s.Office.SaveEval(in, ClientIP(r))
		s.reply(w, http.StatusCreated, e, err)
	}
}

func (s *Server) deleteEval(w http.ResponseWriter, r *http.Request, id string) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusOK, map[string]bool{"deleted": true}, s.Office.DeleteEval(id, ClientIP(r)))
	}
}

func (s *Server) runEvals(w http.ResponseWriter, r *http.Request) {
	var in struct {
		AgentID string   `json:"agent_id"`
		EvalIDs []string `json:"eval_ids"`
	}
	if body(w, r, &in, false) {
		pls, err := s.Office.RunEvals(in.AgentID, in.EvalIDs, ClientIP(r))
		s.reply(w, http.StatusCreated, map[string]any{"pipelines": pls}, err)
	}
}

// Settings and queue.

func (s *Server) updateSettings(w http.ResponseWriter, r *http.Request) {
	var in office.SettingsInput
	if body(w, r, &in, false) {
		st, err := s.Office.UpdateSettings(in, ClientIP(r))
		s.reply(w, http.StatusOK, st, err)
	}
}

// forgetCodex deletes the stored (renewed) copy of Codex's credential.
func (s *Server) forgetCodex(w http.ResponseWriter, r *http.Request) {
	if body(w, r, &struct{}{}, true) {
		s.reply(w, http.StatusOK, map[string]bool{"ok": true}, s.Office.ForgetCodexAuth(ClientIP(r)))
	}
}

// githubRepos lists an owner's GitHub repositories, for importing them
// as projects.
func (s *Server) githubRepos(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), githubCallTimeout)
	defer cancel()
	v, err := s.Office.GitHubRepos(ctx, r.URL.Query().Get("owner"))
	s.reply(w, http.StatusOK, v, err)
}

func (s *Server) pauseQueue(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Paused *bool `json:"paused"`
	}
	if !body(w, r, &in, false) {
		return
	}
	if in.Paused == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "falta paused", "field": "paused"})
		return
	}
	st, err := s.Office.PauseQueue(*in.Paused, ClientIP(r))
	s.reply(w, http.StatusOK, st, err)
}
