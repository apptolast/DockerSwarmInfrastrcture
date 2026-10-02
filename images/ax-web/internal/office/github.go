package office

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"
)

// GitHub broker bounds.
const (
	githubTimeout    = 20 * time.Second
	githubCacheTTL   = 60 * time.Second
	maxGitHubBody    = 2 << 20
	maxPullDiff      = 300 << 10
	maxIssueBody     = 8 << 10
	githubUserAgent  = "apptolast-oficina/1.0"
	githubAPIVersion = "2022-11-28"
	blobWorkers      = 4
)

// Issue is an open issue of a project's repository.
type Issue struct {
	Number  int       `json:"number"`
	Title   string    `json:"title"`
	Labels  []string  `json:"labels"`
	User    string    `json:"user"`
	Created time.Time `json:"created_at"`
	URL     string    `json:"html_url"`
	Body    string    `json:"body"`
}

// Pull is an open pull request of a project's repository.
type Pull struct {
	Number int    `json:"number"`
	Title  string `json:"title"`
	User   string `json:"user"`
	Head   string `json:"head"`
	Base   string `json:"base"`
	Draft  bool   `json:"draft"`
	URL    string `json:"html_url"`
}

// GitHubView is GET /api/projects/{id}/github.
type GitHubView struct {
	Enabled bool    `json:"enabled"`
	Issues  []Issue `json:"issues"`
	Pulls   []Pull  `json:"pulls"`
}

var errNoGitHub = &Error{Status: http.StatusConflict, Message: "GitHub no está configurado para el propietario de este repositorio"}

type ghCache struct {
	at   time.Time
	data any
}

type gitHub struct {
	base   string
	client *http.Client
	tokens func() (ghTokens, error)
	now    func() time.Time

	mu    sync.Mutex
	cache map[string]ghCache
}

func newGitHub(base string, tokens func() (ghTokens, error), now func() time.Time) *gitHub {
	if base == "" {
		base = "https://api.github.com"
	}
	return &gitHub{
		base:   strings.TrimRight(base, "/"),
		client: &http.Client{Timeout: githubTimeout},
		tokens: tokens, now: now, cache: map[string]ghCache{},
	}
}

// ownerRepo reads owner and name from https://github.com/<owner>/<repo>.
func ownerRepo(repo string) (string, string, bool) {
	u, err := url.Parse(repo)
	if err != nil || u.Host != "github.com" {
		return "", "", false
	}
	parts := strings.Split(strings.Trim(u.Path, "/"), "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], strings.TrimSuffix(parts[1], ".git"), true
}

// token is the token for a repository's owner, or "".
func (g *gitHub) token(repo string) (string, string, string) {
	owner, name, ok := ownerRepo(repo)
	if !ok {
		return "", "", ""
	}
	toks, err := g.tokens()
	if err != nil {
		return "", owner, name
	}
	return toks.forOwner(owner), owner, name
}

// enabled reports whether a repository has a token.
func (g *gitHub) enabled(repo string) bool {
	tok, _, _ := g.token(repo)
	return tok != ""
}

type ghStatusError struct{ code int }

func (e *ghStatusError) Error() string { return fmt.Sprintf("GitHub respondió %d", e.code) }

// do sends one request. The token and the response body never reach a
// log or an error.
func (g *gitHub) do(ctx context.Context, token, method, path string, body any, accept string,
	limit int64, truncate bool) ([]byte, error) {
	var rd io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rd = bytes.NewReader(data)
	}
	ctx, cancel := context.WithTimeout(ctx, githubTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, g.base+path, rd)
	if err != nil {
		return nil, err
	}
	if accept == "" {
		accept = "application/vnd.github+json"
	}
	req.Header.Set("Accept", accept)
	req.Header.Set("X-GitHub-Api-Version", githubAPIVersion)
	req.Header.Set("User-Agent", githubUserAgent)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := g.client.Do(req)
	if err != nil {
		return nil, errors.New("no se pudo contactar con GitHub")
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, errors.New("la respuesta de GitHub se cortó")
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, &ghStatusError{code: resp.StatusCode}
	}
	if int64(len(data)) > limit {
		if !truncate {
			return nil, errors.New("la respuesta de GitHub es demasiado grande")
		}
		data = data[:limit]
	}
	return data, nil
}

func (g *gitHub) cached(key string) (any, bool) { return g.cachedFor(key, githubCacheTTL) }

func (g *gitHub) cachedFor(key string, ttl time.Duration) (any, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	c, ok := g.cache[key]
	if !ok || g.now().Sub(c.at) > ttl || g.now().Before(c.at) {
		return nil, false
	}
	return c.data, true
}

func (g *gitHub) store(key string, v any) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if len(g.cache) > 500 {
		g.cache = map[string]ghCache{}
	}
	g.cache[key] = ghCache{at: g.now(), data: v}
}

func ghError(err error) error {
	var se *ghStatusError
	if errors.As(err, &se) {
		switch se.code {
		case http.StatusUnauthorized, http.StatusForbidden:
			return upstream("GitHub rechazó el token (" + fmt.Sprint(se.code) + ")")
		case http.StatusNotFound:
			return upstream("GitHub no encuentra el repositorio o el objeto (404)")
		}
		return upstream(se.Error())
	}
	return upstream(err.Error())
}

func cleanText(s string, maxBytes int) string {
	s, _ = cutBytes(strings.ToValidUTF8(strings.ReplaceAll(s, "\x00", ""), "�"), maxBytes)
	return s
}

// Issues lists a repository's open issues, not pull requests.
func (g *gitHub) Issues(ctx context.Context, repo string) ([]Issue, error) {
	tok, owner, name := g.token(repo)
	if tok == "" {
		return nil, errNoGitHub
	}
	key := "issues " + repo
	if v, ok := g.cached(key); ok {
		return v.([]Issue), nil
	}
	data, err := g.do(ctx, tok, http.MethodGet, "/repos/"+owner+"/"+name+"/issues?state=open&per_page=50", nil, "", maxGitHubBody, false)
	if err != nil {
		return nil, ghError(err)
	}
	var raw []struct {
		Number int    `json:"number"`
		Title  string `json:"title"`
		Labels []struct {
			Name string `json:"name"`
		} `json:"labels"`
		User struct {
			Login string `json:"login"`
		} `json:"user"`
		CreatedAt   time.Time       `json:"created_at"`
		HTMLURL     string          `json:"html_url"`
		Body        *string         `json:"body"`
		PullRequest json.RawMessage `json:"pull_request"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, upstream("GitHub devolvió una respuesta inesperada")
	}
	out := []Issue{}
	for _, r := range raw {
		if len(r.PullRequest) > 0 && string(r.PullRequest) != "null" {
			continue
		}
		is := Issue{Number: r.Number, Title: cleanText(r.Title, 1024), User: cleanText(r.User.Login, 100),
			Created: r.CreatedAt, URL: safeGitHubURL(r.HTMLURL), Labels: []string{}}
		if r.Body != nil {
			is.Body = cleanText(*r.Body, maxIssueBody)
		}
		for _, l := range r.Labels {
			is.Labels = append(is.Labels, cleanText(l.Name, 100))
		}
		out = append(out, is)
	}
	g.store(key, out)
	return out, nil
}

// Pulls lists a repository's open pull requests.
func (g *gitHub) Pulls(ctx context.Context, repo string) ([]Pull, error) {
	tok, owner, name := g.token(repo)
	if tok == "" {
		return nil, errNoGitHub
	}
	key := "pulls " + repo
	if v, ok := g.cached(key); ok {
		return v.([]Pull), nil
	}
	data, err := g.do(ctx, tok, http.MethodGet, "/repos/"+owner+"/"+name+"/pulls?state=open&per_page=50", nil, "", maxGitHubBody, false)
	if err != nil {
		return nil, ghError(err)
	}
	var raw []struct {
		Number int    `json:"number"`
		Title  string `json:"title"`
		User   struct {
			Login string `json:"login"`
		} `json:"user"`
		Head struct {
			Ref string `json:"ref"`
		} `json:"head"`
		Base struct {
			Ref string `json:"ref"`
		} `json:"base"`
		Draft   bool   `json:"draft"`
		HTMLURL string `json:"html_url"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, upstream("GitHub devolvió una respuesta inesperada")
	}
	out := []Pull{}
	for _, r := range raw {
		out = append(out, Pull{Number: r.Number, Title: cleanText(r.Title, 1024), User: cleanText(r.User.Login, 100),
			Head: cleanText(r.Head.Ref, 255), Base: cleanText(r.Base.Ref, 255), Draft: r.Draft, URL: safeGitHubURL(r.HTMLURL)})
	}
	g.store(key, out)
	return out, nil
}

// isNotFound tells a GitHub 404.
func isNotFound(err error) bool {
	var se *ghStatusError
	return errors.As(err, &se) && se.code == http.StatusNotFound
}

// Issue returns one issue (open or closed) with its title and body.
func (g *gitHub) Issue(ctx context.Context, repo string, n int) (Issue, error) {
	tok, owner, name := g.token(repo)
	if tok == "" {
		return Issue{}, errNoGitHub
	}
	data, err := g.do(ctx, tok, http.MethodGet, fmt.Sprintf("/repos/%s/%s/issues/%d", owner, name, n), nil, "", maxGitHubBody, false)
	if isNotFound(err) {
		return Issue{}, notFound(fmt.Sprintf("GitHub no tiene la issue #%d en este repositorio", n))
	}
	if err != nil {
		return Issue{}, ghError(err)
	}
	var r struct {
		Number int    `json:"number"`
		Title  string `json:"title"`
		Labels []struct {
			Name string `json:"name"`
		} `json:"labels"`
		User struct {
			Login string `json:"login"`
		} `json:"user"`
		CreatedAt   time.Time       `json:"created_at"`
		HTMLURL     string          `json:"html_url"`
		Body        *string         `json:"body"`
		PullRequest json.RawMessage `json:"pull_request"`
	}
	if err := json.Unmarshal(data, &r); err != nil || r.Number != n {
		return Issue{}, upstream("GitHub devolvió una respuesta inesperada")
	}
	if len(r.PullRequest) > 0 && string(r.PullRequest) != "null" {
		return Issue{}, conflict(fmt.Sprintf("El #%d es un pull request: elige el origen «pr»", n))
	}
	is := Issue{Number: r.Number, Title: cleanText(r.Title, 1024), User: cleanText(r.User.Login, 100),
		Created: r.CreatedAt, URL: safeGitHubURL(r.HTMLURL), Labels: []string{}}
	if r.Body != nil {
		is.Body = cleanText(*r.Body, maxIssueBody)
	}
	for _, l := range r.Labels {
		is.Labels = append(is.Labels, cleanText(l.Name, 100))
	}
	return is, nil
}

// PullDetail is one pull request with its description.
type PullDetail struct {
	Pull
	Body string `json:"body"`
}

// Pull returns one pull request (open or closed) with its title and body.
func (g *gitHub) Pull(ctx context.Context, repo string, n int) (PullDetail, error) {
	tok, owner, name := g.token(repo)
	if tok == "" {
		return PullDetail{}, errNoGitHub
	}
	data, err := g.do(ctx, tok, http.MethodGet, fmt.Sprintf("/repos/%s/%s/pulls/%d", owner, name, n), nil, "", maxGitHubBody, false)
	if isNotFound(err) {
		return PullDetail{}, notFound(fmt.Sprintf("GitHub no tiene el pull request #%d en este repositorio", n))
	}
	if err != nil {
		return PullDetail{}, ghError(err)
	}
	var r struct {
		Number int    `json:"number"`
		Title  string `json:"title"`
		User   struct {
			Login string `json:"login"`
		} `json:"user"`
		Head struct {
			Ref string `json:"ref"`
		} `json:"head"`
		Base struct {
			Ref string `json:"ref"`
		} `json:"base"`
		Draft   bool    `json:"draft"`
		HTMLURL string  `json:"html_url"`
		Body    *string `json:"body"`
	}
	if err := json.Unmarshal(data, &r); err != nil || r.Number != n {
		return PullDetail{}, upstream("GitHub devolvió una respuesta inesperada")
	}
	out := PullDetail{Pull: Pull{Number: r.Number, Title: cleanText(r.Title, 1024), User: cleanText(r.User.Login, 100),
		Head: cleanText(r.Head.Ref, 255), Base: cleanText(r.Base.Ref, 255), Draft: r.Draft, URL: safeGitHubURL(r.HTMLURL)}}
	if r.Body != nil {
		out.Body = cleanText(*r.Body, maxIssueBody)
	}
	return out, nil
}

// PullDiff is a pull request's diff, cut at 300 KiB.
func (g *gitHub) PullDiff(ctx context.Context, repo string, n int) (string, bool, error) {
	tok, owner, name := g.token(repo)
	if tok == "" {
		return "", false, errNoGitHub
	}
	data, err := g.do(ctx, tok, http.MethodGet, fmt.Sprintf("/repos/%s/%s/pulls/%d", owner, name, n), nil,
		"application/vnd.github.diff", maxPullDiff, true)
	if err != nil {
		return "", false, ghError(err)
	}
	return cleanText(string(data), maxPullDiff), len(data) >= maxPullDiff, nil
}

// Comment posts a comment on an issue or pull request.
func (g *gitHub) Comment(ctx context.Context, repo string, n int, body string) (string, error) {
	tok, owner, name := g.token(repo)
	if tok == "" {
		return "", errNoGitHub
	}
	body, _ = cutBytes(body, maxGitHubBodyBytes)
	data, err := g.do(ctx, tok, http.MethodPost, fmt.Sprintf("/repos/%s/%s/issues/%d/comments", owner, name, n),
		map[string]string{"body": body}, "", maxGitHubBody, false)
	if err != nil {
		return "", ghError(err)
	}
	var out struct {
		HTMLURL string `json:"html_url"`
	}
	_ = json.Unmarshal(data, &out)
	return safeGitHubURL(out.HTMLURL), nil
}

// safeGitHubURL keeps only https://github.com links.
func safeGitHubURL(s string) string {
	if strings.HasPrefix(s, "https://github.com/") && utf8.ValidString(s) && len(s) < 500 {
		return s
	}
	return ""
}

// validTreePath refuses paths a tree must never get from a sandbox.
func validTreePath(p string) bool {
	if p == "" || len(p) > 4096 || strings.HasPrefix(p, "/") || strings.ContainsAny(p, "\x00\\") || !utf8.ValidString(p) {
		return false
	}
	for _, seg := range strings.Split(p, "/") {
		if seg == "" || seg == "." || seg == ".." || strings.EqualFold(seg, ".git") {
			return false
		}
	}
	return true
}

// prRequest is what CreatePR builds a pull request from.
type prRequest struct {
	Repo     string
	BaseSHA  string
	Branch   string // the base branch
	Head     string // oficina/<job id>
	Title    string
	Message  string // commit message
	Body     string
	Contents []contentEntry
}

type treeEntry struct {
	Path string  `json:"path"`
	Mode string  `json:"mode"`
	Type string  `json:"type"`
	SHA  *string `json:"sha"`
}

// CreatePR builds a commit through the Git Data API (blobs, tree,
// commit, ref) on top of the job's base commit and opens a draft pull
// request from it. A retry after a partial failure moves the branch.
func (g *gitHub) CreatePR(ctx context.Context, r prRequest) (PullRequest, error) {
	tok, owner, name := g.token(r.Repo)
	if tok == "" {
		return PullRequest{}, errNoGitHub
	}
	if len(r.Contents) == 0 {
		return PullRequest{}, conflict("El trabajo no tiene cambios que proponer")
	}
	for _, c := range r.Contents {
		if !validTreePath(c.Path) {
			return PullRequest{}, conflict("Los cambios tienen una ruta no válida para GitHub")
		}
		if msg := protectedPath(c.Path); msg != "" {
			return PullRequest{}, conflict(msg)
		}
		if !c.Deleted && c.Mode != "100644" && c.Mode != "100755" && c.Mode != "120000" {
			return PullRequest{}, conflict("Los cambios tienen un modo de fichero no admitido")
		}
	}
	repoPath := "/repos/" + owner + "/" + name
	call := func(method, path string, body any, out any) error {
		data, err := g.do(ctx, tok, method, repoPath+path, body, "", maxGitHubBody, false)
		if err != nil {
			return err
		}
		if out != nil && json.Unmarshal(data, out) != nil {
			return errors.New("GitHub devolvió una respuesta inesperada")
		}
		return nil
	}
	var commit struct {
		Tree struct {
			SHA string `json:"sha"`
		} `json:"tree"`
	}
	if err := call(http.MethodGet, "/git/commits/"+r.BaseSHA, nil, &commit); err != nil || commit.Tree.SHA == "" {
		if err == nil {
			err = errors.New("GitHub no devolvió el árbol del commit base")
		}
		return PullRequest{}, ghError(err)
	}
	// Blobs, a few at a time, in the order of the contents.
	shas := make([]string, len(r.Contents))
	type job struct{ i int }
	jobs := make(chan job)
	errs := make(chan error, len(r.Contents))
	var wg sync.WaitGroup
	for range min(blobWorkers, len(r.Contents)) {
		wg.Go(func() {
			for j := range jobs {
				c := r.Contents[j.i]
				var blob struct {
					SHA string `json:"sha"`
				}
				err := call(http.MethodPost, "/git/blobs", map[string]string{
					"content": base64.StdEncoding.EncodeToString(c.Data), "encoding": "base64",
				}, &blob)
				if err == nil && blob.SHA == "" {
					err = errors.New("GitHub no devolvió el blob")
				}
				if err != nil {
					errs <- err
					continue
				}
				shas[j.i] = blob.SHA
			}
		})
	}
	for i, c := range r.Contents {
		if !c.Deleted {
			jobs <- job{i}
		}
	}
	close(jobs)
	wg.Wait()
	close(errs)
	if err := <-errs; err != nil {
		return PullRequest{}, ghError(err)
	}
	entries := make([]treeEntry, 0, len(r.Contents))
	for i, c := range r.Contents {
		if c.Deleted {
			entries = append(entries, treeEntry{Path: c.Path, Mode: "100644", Type: "blob", SHA: nil})
			continue
		}
		sha := shas[i]
		entries = append(entries, treeEntry{Path: c.Path, Mode: c.Mode, Type: "blob", SHA: &sha})
	}
	var tree, created struct {
		SHA string `json:"sha"`
	}
	if err := call(http.MethodPost, "/git/trees", map[string]any{"base_tree": commit.Tree.SHA, "tree": entries}, &tree); err != nil {
		return PullRequest{}, ghError(err)
	}
	if err := call(http.MethodPost, "/git/commits", map[string]any{
		"message": r.Message, "tree": tree.SHA, "parents": []string{r.BaseSHA},
	}, &created); err != nil {
		return PullRequest{}, ghError(err)
	}
	err := call(http.MethodPost, "/git/refs", map[string]string{"ref": "refs/heads/" + r.Head, "sha": created.SHA}, nil)
	var se *ghStatusError
	if errors.As(err, &se) && se.code == http.StatusUnprocessableEntity {
		err = call(http.MethodPatch, "/git/refs/heads/"+r.Head, map[string]any{"sha": created.SHA, "force": true}, nil)
	}
	if err != nil {
		return PullRequest{}, ghError(err)
	}
	var pr struct {
		Number  int    `json:"number"`
		HTMLURL string `json:"html_url"`
	}
	body, _ := cutBytes(r.Body, maxGitHubBodyBytes)
	err = call(http.MethodPost, "/pulls", map[string]any{
		"title": r.Title, "head": r.Head, "base": r.Branch, "body": body, "draft": true,
	}, &pr)
	if errors.As(err, &se) && se.code == http.StatusUnprocessableEntity {
		// A pull request for this branch already exists (an earlier try
		// opened it and then failed to record it): it is this job's.
		var open []struct {
			Number  int    `json:"number"`
			HTMLURL string `json:"html_url"`
		}
		q := "/pulls?state=open&head=" + url.QueryEscape(owner+":"+r.Head)
		if lerr := call(http.MethodGet, q, nil, &open); lerr == nil && len(open) > 0 && open[0].Number > 0 {
			pr.Number, pr.HTMLURL, err = open[0].Number, open[0].HTMLURL, nil
		}
	}
	if err != nil {
		return PullRequest{}, ghError(err)
	}
	return PullRequest{Number: pr.Number, URL: safeGitHubURL(pr.HTMLURL), Branch: r.Head}, nil
}

// Repository listing of an owner, for importing projects.
const (
	githubReposTTL   = 5 * time.Minute
	githubReposPages = 3
	githubReposPage  = 100
)

var githubOwnerRe = regexp.MustCompile(`^[A-Za-z0-9-]{1,39}$`)

// GitHubRepo is one repository of an owner.
type GitHubRepo struct {
	Name          string     `json:"name"`
	FullName      string     `json:"full_name"`
	HTMLURL       string     `json:"html_url"`
	Description   string     `json:"description"`
	DefaultBranch string     `json:"default_branch"`
	Private       bool       `json:"private"`
	Archived      bool       `json:"archived"`
	Fork          bool       `json:"fork"`
	PushedAt      *time.Time `json:"pushed_at"`
	Language      string     `json:"language"`
}

// GitHubRepos is GET /api/github/repos?owner=: an organization's (or a
// user's) repositories, with the office's token for that owner when it
// has one (Authenticated), else the public ones without a token.
type GitHubRepos struct {
	Owner         string       `json:"owner"`
	Authenticated bool         `json:"authenticated"`
	Repos         []GitHubRepo `json:"repos"`
}

var repoNameRe = regexp.MustCompile(`^[A-Za-z0-9._-]{1,100}$`)

// oneLine makes a GitHub text one line of at most runes characters (the
// cut mark included) without control characters, as a project's fields
// must be.
func oneLine(s string, runes int) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return ' '
		}
		return r
	}, cleanText(s, 4<<10))
	s = strings.Join(strings.Fields(s), " ")
	if utf8.RuneCountInString(s) > runes {
		s = truncRunes(s, runes-1)
	}
	return s
}

// Repos lists up to githubReposPages pages of an owner's repositories:
// /orgs/{owner}/repos, or /users/{owner}/repos when GitHub does not know
// the organization. Cached githubReposTTL per owner.
func (g *gitHub) Repos(ctx context.Context, owner string) (GitHubRepos, error) {
	if !githubOwnerRe.MatchString(owner) {
		return GitHubRepos{}, fieldErr("owner", "El propietario de GitHub no es válido (letras, números y guiones, hasta 39)")
	}
	tok := ""
	if toks, err := g.tokens(); err == nil {
		tok = toks.forOwner(owner)
	}
	key := "repos " + strings.ToLower(owner) + " " + fmt.Sprint(tok != "")
	if v, ok := g.cachedFor(key, githubReposTTL); ok {
		return v.(GitHubRepos), nil
	}
	out := GitHubRepos{Owner: owner, Authenticated: tok != "", Repos: []GitHubRepo{}}
	base := "/orgs/" + owner + "/repos?per_page=100&type=all"
	for page := 1; page <= githubReposPages; page++ {
		data, err := g.do(ctx, tok, http.MethodGet, base+"&page="+fmt.Sprint(page), nil, "", maxGitHubBody, false)
		if page == 1 && isNotFound(err) {
			base = "/users/" + owner + "/repos?per_page=100&type=owner"
			data, err = g.do(ctx, tok, http.MethodGet, base+"&page=1", nil, "", maxGitHubBody, false)
		}
		switch {
		case isNotFound(err):
			return GitHubRepos{}, notFound("GitHub no conoce a ese propietario")
		case err != nil:
			var se *ghStatusError
			if tok == "" && errors.As(err, &se) && (se.code == http.StatusForbidden || se.code == http.StatusTooManyRequests) {
				return GitHubRepos{}, upstream("GitHub limitó las consultas sin token; inténtalo más tarde")
			}
			return GitHubRepos{}, ghError(err)
		}
		var raw []struct {
			Name          string  `json:"name"`
			FullName      string  `json:"full_name"`
			Description   *string `json:"description"`
			DefaultBranch string  `json:"default_branch"`
			Private       bool    `json:"private"`
			Archived      bool    `json:"archived"`
			Fork          bool    `json:"fork"`
			PushedAt      *string `json:"pushed_at"`
			Language      *string `json:"language"`
			Owner         struct {
				Login string `json:"login"`
			} `json:"owner"`
		}
		if err := json.Unmarshal(data, &raw); err != nil {
			return GitHubRepos{}, upstream("GitHub devolvió una respuesta inesperada")
		}
		for _, r := range raw {
			login := r.Owner.Login
			if !githubOwnerRe.MatchString(login) || !repoNameRe.MatchString(r.Name) {
				continue
			}
			// The link is built, never copied: always a plain
			// https://github.com/<owner>/<repo>.
			repo := GitHubRepo{Name: r.Name, FullName: login + "/" + r.Name,
				HTMLURL: "https://github.com/" + login + "/" + r.Name, DefaultBranch: cleanText(r.DefaultBranch, 255),
				Private: r.Private, Archived: r.Archived, Fork: r.Fork}
			if r.Description != nil {
				repo.Description = oneLine(*r.Description, MaxDescriptionRunes)
			}
			if r.Language != nil {
				repo.Language = oneLine(*r.Language, 50)
			}
			if r.PushedAt != nil {
				if t, err := time.Parse(time.RFC3339, *r.PushedAt); err == nil {
					t = t.UTC()
					repo.PushedAt = &t
				}
			}
			out.Repos = append(out.Repos, repo)
		}
		if len(raw) < githubReposPage {
			break
		}
	}
	g.store(key, out)
	return out, nil
}
