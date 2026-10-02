package demo

import (
	"encoding/json"
	"net/http"
	"strings"
)

// fakeGitHub stands in for api.github.com in the demo, on loopback: it
// answers the only calls the demo can make without a token, the listing
// of an owner's repositories (GET /orgs/{owner}/repos and
// /users/{owner}/repos), with a few made-up ones. Anything else is 404,
// as GitHub answers what it does not have.
func fakeGitHub() http.Handler {
	mux := http.NewServeMux()
	list := func(w http.ResponseWriter, r *http.Request) {
		owner := r.PathValue("owner")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		if p := r.URL.Query().Get("page"); p != "" && p != "1" {
			_, _ = w.Write([]byte("[]"))
			return
		}
		_ = json.NewEncoder(w).Encode(fakeRepos(owner))
	}
	mux.HandleFunc("GET /orgs/{owner}/repos", list)
	mux.HandleFunc("GET /users/{owner}/repos", list)
	return mux
}

// fakeRepos are an owner's made-up repositories, shaped like GitHub's.
func fakeRepos(owner string) []map[string]any {
	type repo struct {
		name, desc, branch, lang, pushed string
		private, archived, fork          bool
	}
	repos := []repo{
		{"api", "API de ejemplo de la demo.", "main", "Go", "2026-09-30T18:12:00Z", false, false, false},
		{"web", "Web pública de ejemplo.", "develop", "TypeScript", "2026-09-28T09:40:00Z", false, false, false},
		{"docs", "Documentación de ejemplo.", "main", "", "2026-08-02T11:00:00Z", false, false, false},
		{"secretos", "Un repositorio privado: AX no puede clonarlo.", "main", "Shell", "2026-09-01T07:00:00Z", true, false, false},
		{"legado", "Un repositorio archivado.", "master", "Java", "2024-01-15T10:00:00Z", false, true, false},
		{"grpc-go", "Un fork de ejemplo.", "master", "Go", "2026-07-20T16:30:00Z", false, false, true},
	}
	if strings.EqualFold(owner, "apptolast") {
		repos = append([]repo{
			{"DockerSwarmInfrastrcture", "Infraestructura como código del servidor de AppToLast.", "main", "Python", "2026-10-02T08:00:00Z", false, false, false},
			{"DockerSwarmDocs", "Documentación de la infraestructura.", "main", "", "2026-09-29T12:00:00Z", false, false, false},
		}, repos...)
	}
	out := make([]map[string]any, 0, len(repos))
	for _, r := range repos {
		var lang any
		if r.lang != "" {
			lang = r.lang
		}
		out = append(out, map[string]any{
			"name": r.name, "full_name": owner + "/" + r.name, "html_url": "https://github.com/" + owner + "/" + r.name,
			"description": r.desc, "default_branch": r.branch, "private": r.private, "archived": r.archived,
			"fork": r.fork, "pushed_at": r.pushed, "language": lang, "owner": map[string]string{"login": owner},
		})
	}
	return out
}
