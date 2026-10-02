package runs

import (
	"bytes"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"unicode/utf8"

	"apptolast.com/ax-web/internal/fakeax"
	"apptolast.com/ax-web/internal/harness"
)

func TestParseChanges(t *testing.T) {
	numstat := "10\t0\tdocs/a b.md\x00" + // a space in the path
		"-\t-\tlogo.png\x00" +
		"2\t3\t\x00src/old.go\x00src/new.go\x00" + // a rename
		"4\t0\t\x00tpl/x.txt\x00tpl/y.txt\x00" + // a copy
		"0\t7\tgone\x00" +
		"1\t1\tscript\x00"
	names := "A\x00docs/a b.md\x00A\x00logo.png\x00R075\x00src/old.go\x00src/new.go\x00" +
		"C100\x00tpl/x.txt\x00tpl/y.txt\x00D\x00gone\x00T\x00script\x00"
	files, err := parseChanges([]byte(numstat), []byte(names))
	if err != nil {
		t.Fatal(err)
	}
	want := []harness.ChangedFile{
		{Path: "docs/a b.md", Status: "A", Additions: 10},
		{Path: "logo.png", Status: "A", Binary: true},
		{Path: "src/new.go", OldPath: "src/old.go", Status: "R", Additions: 2, Deletions: 3},
		{Path: "tpl/y.txt", OldPath: "tpl/x.txt", Status: "C", Additions: 4},
		{Path: "gone", Status: "D", Deletions: 7},
		{Path: "script", Status: "T", Additions: 1, Deletions: 1},
	}
	if !slices.Equal(files, want) {
		t.Fatalf("%+v", files)
	}
	if files, err := parseChanges(nil, nil); err != nil || len(files) != 0 {
		t.Fatalf("empty: %v %v", files, err)
	}
	for name, c := range map[string][2]string{
		"no tabs":         {"x\x00", ""},
		"bad count":       {"a\tb\tx\x00", ""},
		"short rename":    {"1\t1\t\x00old\x00", ""},
		"no path":         {"", "M\x00"},
		"unknown status":  {"", "Z\x00x\x00"},
		"short rename ns": {"", "R100\x00old\x00"},
	} {
		if _, err := parseChanges([]byte(c[0]), []byte(c[1])); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestParseArchive(t *testing.T) {
	data := fakeax.Tar(
		fakeax.TarFile{Name: "a", Dir: true},
		fakeax.TarFile{Name: "a/x.go", Body: "package x\n"},
		fakeax.TarFile{Name: "a/run", Body: "#!/bin/sh\n", Mode: 0o755},
		fakeax.TarFile{Name: "a/link", Link: "x.go"},
		fakeax.TarFile{Name: "other", Body: "not asked for"},
		fakeax.TarFile{Name: "a/" + string(bytes.Repeat([]byte("n"), 150)), Body: "long name, PAX"},
	)
	long := "a/" + string(bytes.Repeat([]byte("n"), 150))
	got, complete, err := parseArchive(data, []string{"a/x.go", "a/run", "a/link", long}, 1<<20)
	if err != nil || !complete || len(got) != 4 {
		t.Fatalf("%v %v %+v", err, complete, got)
	}
	for path, want := range map[string]harness.FileContent{
		"a/x.go": {Path: "a/x.go", Mode: "100644", Data: []byte("package x\n")},
		"a/run":  {Path: "a/run", Mode: "100755", Data: []byte("#!/bin/sh\n")},
		"a/link": {Path: "a/link", Mode: "120000", Data: []byte("x.go")},
		long:     {Path: long, Mode: "100644", Data: []byte("long name, PAX")},
	} {
		if c := got[path]; c.Path != want.Path || c.Mode != want.Mode || !bytes.Equal(c.Data, want.Data) {
			t.Errorf("%s: %+v", path, c)
		}
	}
	// Over the bound, a directory where a file was asked for, a path the
	// archive lacks: incomplete, no contents, no error.
	for name, c := range map[string]struct {
		paths []string
		max   int
	}{
		"too big":   {[]string{"a/x.go", "a/run"}, 15},
		"directory": {[]string{"a/x.go", "a"}, 1 << 20},
		"missing":   {[]string{"a/x.go", "b"}, 1 << 20},
	} {
		got, complete, err := parseArchive(data, c.paths, c.max)
		if err != nil || complete || got != nil {
			t.Errorf("%s: %v %v %+v", name, err, complete, got)
		}
	}
	if _, _, err := parseArchive(bytes.Repeat([]byte("z"), 1024), []string{"a"}, 1<<20); err == nil {
		t.Error("garbage accepted")
	}
}

// A change list with too many entries or an absurd path is refused as a
// whole: the capture fails with "demasiados cambios" and keeps no list.
func TestParseChangesBounds(t *testing.T) {
	var names, numstat strings.Builder
	for i := range maxChangedFiles {
		fmt.Fprintf(&names, "A\x00f%d\x00", i)
		fmt.Fprintf(&numstat, "1\t0\tf%d\x00", i)
	}
	files, err := parseChanges([]byte(numstat.String()), []byte(names.String()))
	if err != nil || len(files) != maxChangedFiles {
		t.Fatalf("%d files: %v", len(files), err)
	}
	names.WriteString("A\x00one-more\x00")
	if _, err := parseChanges([]byte(numstat.String()), []byte(names.String())); !errors.Is(err, errTooManyChanges) ||
		!strings.Contains(err.Error(), "demasiados cambios") {
		t.Fatalf("over the bound: %v", err)
	}
	long := strings.Repeat("d/", maxChangedPath/2) + "x"
	if _, err := parseChanges(nil, []byte("A\x00"+long+"\x00")); !errors.Is(err, errTooManyChanges) {
		t.Fatalf("long path: %v", err)
	}
}

func TestBoundEvent(t *testing.T) {
	ev := boundEvent(harness.Event{Kind: harness.EventTool, Text: strings.Repeat("é", harness.MaxEventText),
		Input: strings.Repeat("i", 5000), Model: strings.Repeat("m", 500), Tool: strings.Repeat("t", 500)})
	if len(ev.Text) > harness.MaxEventText || len(ev.Input) > harness.MaxEventInput || len(ev.Model) > maxEventName ||
		len(ev.Tool) > maxEventName || !utf8.ValidString(ev.Text) {
		t.Fatalf("%d %d %d %d", len(ev.Text), len(ev.Input), len(ev.Model), len(ev.Tool))
	}
	small := harness.Event{Kind: harness.EventText, Text: "hola", Tool: "Bash"}
	if boundEvent(small) != small {
		t.Fatal("a small event changed")
	}
}
