package office

import (
	"sync"
	"testing"
)

// A URL already matching a project (case or a trailing .git aside) finds
// it instead of creating a duplicate, through the real find-or-create
// path used by the job form.
func TestFindOrCreateProjectByRepoIdempotent(t *testing.T) {
	h := newHarness(t)
	before := len(h.o.Snapshot().Projects)
	first, err := h.o.FindOrCreateProjectByRepo("https://github.com/ana/nuevo", "", "x")
	if err != nil {
		t.Fatal(err)
	}
	if len(h.o.Snapshot().Projects) != before+1 {
		t.Fatalf("project not created: %+v", h.o.Snapshot().Projects)
	}
	variants := []string{
		"https://github.com/ana/nuevo",
		"https://github.com/ana/nuevo.git",
		"https://github.com/Ana/Nuevo",
		"https://github.com/ANA/NUEVO.GIT",
	}
	for _, v := range variants {
		got, err := h.o.FindOrCreateProjectByRepo(v, "", "x")
		if err != nil {
			t.Fatalf("%s: %v", v, err)
		}
		if got.ID != first.ID {
			t.Fatalf("%s: got project %q, want %q", v, got.ID, first.ID)
		}
		if n := len(h.o.Snapshot().Projects); n != before+1 {
			t.Fatalf("%s: project count %d, want %d (a duplicate was created)", v, n, before+1)
		}
	}
}

// Concurrent calls for the same new repository still create exactly one
// project: the whole find-or-create happens under o.mu, so there is no
// window between "not found" and "created" for a second caller to land
// in.
func TestFindOrCreateProjectByRepoIsAtomicUnderConcurrency(t *testing.T) {
	h := newHarness(t)
	before := len(h.o.Snapshot().Projects)
	const n = 8
	ids := make([]string, n)
	errs := make([]error, n)
	var wg sync.WaitGroup
	for i := range ids {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			p, err := h.o.FindOrCreateProjectByRepo("https://github.com/ana/carrera", "", "x")
			ids[i], errs[i] = p.ID, err
		}(i)
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("call %d: %v", i, err)
		}
		if ids[i] == "" || ids[i] != ids[0] {
			t.Fatalf("call %d: id %q, want all equal to %q", i, ids[i], ids[0])
		}
	}
	if got := len(h.o.Snapshot().Projects); got != before+1 {
		t.Fatalf("project count %d, want %d (a race created a duplicate)", got, before+1)
	}
}

// A seeded project already covering the repository is reused as-is: no
// new project, and the seeded one keeps its own branch regardless of what
// the caller passed.
func TestFindOrCreateProjectByRepoReusesSeededProject(t *testing.T) {
	h := newHarness(t)
	before := len(h.o.Snapshot().Projects)
	got, err := h.o.FindOrCreateProjectByRepo("https://github.com/apptolast/WEB.git", "main", "x")
	if err != nil {
		t.Fatal(err)
	}
	if got.ID != "web" || !got.Seeded || got.Branch != "develop" {
		t.Fatalf("%+v", got)
	}
	if n := len(h.o.Snapshot().Projects); n != before {
		t.Fatalf("project count %d, want %d (no project should be created)", n, before)
	}
}

// Every way ValidateRepo rejects a URL fails FindOrCreateProjectByRepo
// with the same field and message CreateProject already gives: this
// endpoint does not relax that validation.
func TestFindOrCreateProjectByRepoRejectsWhatCreateProjectRejects(t *testing.T) {
	h := newHarness(t)
	cases := []string{
		"https://evil.example.com/ana/repo", // host not allowed
		"https://user@github.com/ana/repo",  // userinfo
		"https://github.com:8443/ana/repo",  // port
		"https://github.com/ana/repo?x=1",   // query
		"https://github.com/ana",            // missing name segment
		"ftp://github.com/ana/repo",         // not https
		"",                                  // empty
	}
	for _, repo := range cases {
		_, wantErr := h.o.CreateProject(ProjectInput{Name: ptr("N"), Repo: ptr(repo)}, "x")
		_, gotErr := h.o.FindOrCreateProjectByRepo(repo, "", "x")
		var wantFE, gotFE *FieldError
		if !asField(wantErr, &wantFE) {
			t.Fatalf("%q: CreateProject did not fail with a field error: %v", repo, wantErr)
		}
		if !asField(gotErr, &gotFE) {
			t.Fatalf("%q: FindOrCreateProjectByRepo did not fail with a field error: %v", repo, gotErr)
		}
		if gotFE.Field != wantFE.Field || gotFE.Message != wantFE.Message {
			t.Fatalf("%q: got %+v, want %+v", repo, gotFE, wantFE)
		}
	}
}

// An invalid branch fails the same way UpdateProject/CreateJob already
// report it, and never creates a project.
func TestFindOrCreateProjectByRepoRejectsBadBranch(t *testing.T) {
	h := newHarness(t)
	before := len(h.o.Snapshot().Projects)
	_, err := h.o.FindOrCreateProjectByRepo("https://github.com/ana/otro", "../etc", "x")
	var fe *FieldError
	if !asField(err, &fe) || fe.Field != "branch" {
		t.Fatalf("%v", err)
	}
	if n := len(h.o.Snapshot().Projects); n != before {
		t.Fatalf("project count %d, want %d (a bad branch must not create a project)", n, before)
	}
}
