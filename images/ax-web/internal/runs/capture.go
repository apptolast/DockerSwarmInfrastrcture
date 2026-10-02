package runs

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strconv"
	"strings"

	"apptolast.com/ax-web/internal/harness"
)

// Capture bounds besides harness's: the file lists and the archive.
const (
	maxListBytes = 1 << 20
	// maxChangedFiles and maxChangedPath bound the change list: beyond
	// them the capture fails ("demasiados cambios") and no list is kept.
	maxChangedFiles = 4000
	maxChangedPath  = 4096
	// tarOverhead is an archive's bytes per file besides its contents:
	// its header, a possible PAX header, padding and a parent directory.
	tarOverhead = 4 << 10
)

var objectName = regexp.MustCompile(`^[0-9a-f]{40}$`)

// errTooManyChanges refuses a change list over maxChangedFiles entries or
// with a path over maxChangedPath bytes.
var errTooManyChanges = errors.New("demasiados cambios")

// limits are the capture's bounds; tests shrink them.
type limits struct {
	patch        int
	contents     int
	contentFiles int
	list         int
	codexAuth    int
}

func defaultLimits() limits {
	return limits{
		patch: harness.MaxPatchBytes, contents: harness.MaxContentsBytes,
		contentFiles: harness.MaxContentFiles, list: maxListBytes, codexAuth: MaxCodexAuth,
	}
}

func gitArgv(args ...string) []string {
	return append([]string{"git", "-C", RepoPath}, args...)
}

// CaptureCommands are the fixed argv of the capture, against base: stage
// everything (with no hooks), then the binary patch, its numstat and its
// name-status, all with no external diff or textconv program.
func CaptureCommands(base string) (add, patch, numstat, nameStatus []string) {
	diff := []string{"diff", "--cached", "--no-ext-diff", "--no-textconv"}
	return gitArgv("-c", "core.hooksPath=/dev/null", "add", "-A"),
		gitArgv(append(diff, "--no-color", "--binary", "--full-index", "-M", base)...),
		gitArgv(append(diff, "--numstat", "-z", "-M", base)...),
		gitArgv(append(diff, "--name-status", "-z", "-M", base)...)
}

// ArchiveCommand writes the given paths of tree as a tar stream; every
// path is a literal pathspec after "--".
func ArchiveCommand(tree string, paths []string) []string {
	argv := gitArgv("archive", "--format=tar", tree, "--")
	for _, p := range paths {
		argv = append(argv, ":(literal)"+p)
	}
	return argv
}

// ApplyCommand applies a patch read from stdin to the checkout and the
// index.
func ApplyCommand() []string {
	return gitArgv("apply", "--index", "--whitespace=nowarn", "-")
}

// capture collects the checkout's changes against the base commit. It
// never fails the run: whatever went wrong is in Changes.Error, and what
// was collected before stays.
func (m *Manager) capture(ctx context.Context, r *Run, proc ProcessClient) *harness.Changes {
	r.mu.Lock()
	base := r.base
	r.mu.Unlock()
	ch := &harness.Changes{BaseSHA: base}
	fail := func(format string, args ...any) *harness.Changes {
		ch.Error = fmt.Sprintf(format, args...)
		ch.Contents, ch.ContentsComplete = nil, false
		r.warn("no se pudieron recoger todos los cambios: %s", ch.Error)
		return ch
	}
	timeout := m.commandTimeout()
	r.system("Recogiendo los cambios del árbol de trabajo…")
	add, patchCmd, numstatCmd, namesCmd := CaptureCommands(base)
	o, err := m.run(ctx, proc, add, nil, maxDetail, timeout)
	if err != nil || o.code != 0 {
		return fail("git add falló%s", quote(o, err))
	}
	o, err = m.run(ctx, proc, patchCmd, nil, m.lim.patch, timeout)
	if err != nil || (!o.cut && o.code != 0) {
		return fail("git diff falló%s", quote(o, err))
	}
	ch.Patch, ch.PatchTruncated = o.stdout, o.cut
	numstat, err := m.run(ctx, proc, numstatCmd, nil, m.lim.list, timeout)
	if err == nil && numstat.cut {
		return fail("%v: la lista supera %d KiB", errTooManyChanges, m.lim.list>>10)
	}
	if err != nil || numstat.code != 0 {
		return fail("no se pudo listar los cambios (numstat)%s", quote(numstat, err))
	}
	names, err := m.run(ctx, proc, namesCmd, nil, m.lim.list, timeout)
	if err == nil && names.cut {
		return fail("%v: la lista supera %d KiB", errTooManyChanges, m.lim.list>>10)
	}
	if err != nil || names.code != 0 {
		return fail("no se pudo listar los cambios (name-status)%s", quote(names, err))
	}
	files, err := parseChanges(numstat.stdout, names.stdout)
	switch {
	case errors.Is(err, errTooManyChanges):
		return fail("%v", err)
	case err != nil:
		return fail("la lista de cambios no se entiende: %v", err)
	}
	ch.Files = files
	if len(files) == 0 {
		ch.ContentsComplete = true
		r.system("Sin cambios en el árbol de trabajo.")
		return ch
	}
	adds, dels := 0, 0
	for _, f := range files {
		adds += f.Additions
		dels += f.Deletions
	}
	note := ""
	if ch.PatchTruncated {
		note = fmt.Sprintf("; el parche supera %d MiB y se ha truncado", m.lim.patch>>20)
	}
	r.system("Cambios: %d ficheros (+%d −%d)%s.", len(files), adds, dels, note)
	if ch.PatchTruncated || len(files) > m.lim.contentFiles {
		r.system("No se guarda el contenido de los ficheros: son demasiados cambios para una pull request.")
		return ch
	}

	tree, err := m.run(ctx, proc, gitArgv("write-tree"), nil, maxDetail, timeout)
	sha := strings.TrimSpace(string(tree.stdout))
	if err != nil || tree.code != 0 || !objectName.MatchString(sha) {
		return fail("git write-tree falló%s", quote(tree, err))
	}
	var paths []string
	for _, f := range files {
		if f.Status != "D" {
			paths = append(paths, f.Path)
		}
	}
	got := map[string]harness.FileContent{}
	complete := true
	if len(paths) > 0 {
		arch, err := m.run(ctx, proc, ArchiveCommand(sha, paths), nil,
			m.lim.contents+(len(paths)+16)*tarOverhead, timeout)
		switch {
		case err != nil || (!arch.cut && arch.code != 0):
			return fail("git archive falló%s", quote(arch, err))
		case arch.cut:
			complete = false
		default:
			got, complete, err = parseArchive(arch.stdout, paths, m.lim.contents)
			if err != nil {
				return fail("el archivo tar no se entiende: %v", err)
			}
		}
	}
	if !complete {
		r.system("No se guarda el contenido de los ficheros: es demasiado grande o incluye algo que no es un fichero (un submódulo).")
		return ch
	}
	for _, f := range files {
		switch f.Status {
		case "D":
			ch.Contents = append(ch.Contents, harness.FileContent{Path: f.Path, Deleted: true})
			continue
		case "R":
			ch.Contents = append(ch.Contents, harness.FileContent{Path: f.OldPath, Deleted: true})
		}
		ch.Contents = append(ch.Contents, got[f.Path])
	}
	ch.ContentsComplete = true
	return ch
}

// quote is the start of a failed command's output, for Changes.Error.
func quote(o output, err error) string {
	if err != nil {
		return ": no se pudo ejecutar en el sandbox"
	}
	if d := strings.TrimSpace(string(o.detail)); d != "" {
		return ": " + harness.Clip(d, 300)
	}
	if o.code >= 0 {
		return fmt.Sprintf(" (código %d)", o.code)
	}
	return ""
}

// parseChanges merges `git diff --numstat -z` and `--name-status -z`
// (with -M) into the change list, in name-status order. More than
// maxChangedFiles entries, or a path over maxChangedPath bytes, is
// errTooManyChanges.
func parseChanges(numstat, nameStatus []byte) ([]harness.ChangedFile, error) {
	for _, p := range bytes.Split(nameStatus, []byte{0}) {
		if len(p) > maxChangedPath {
			return nil, fmt.Errorf("%w: una ruta supera %d bytes", errTooManyChanges, maxChangedPath)
		}
	}
	type stat struct {
		add, del int
		binary   bool
	}
	stats := map[string]stat{}
	tokens := strings.Split(string(numstat), "\x00")
	for i := 0; i < len(tokens); {
		tok := tokens[i]
		i++
		if tok == "" {
			continue
		}
		parts := strings.SplitN(tok, "\t", 3)
		if len(parts) != 3 {
			return nil, errors.New("línea de numstat inesperada")
		}
		path := parts[2]
		if path == "" {
			// A rename or copy: the old and the new path follow.
			if i+1 >= len(tokens) || tokens[i] == "" || tokens[i+1] == "" {
				return nil, errors.New("numstat de un renombrado incompleto")
			}
			path = tokens[i+1]
			i += 2
		}
		var s stat
		if parts[0] == "-" && parts[1] == "-" {
			s.binary = true
		} else {
			a, errA := strconv.Atoi(parts[0])
			d, errD := strconv.Atoi(parts[1])
			if errA != nil || errD != nil {
				return nil, errors.New("recuento de numstat inesperado")
			}
			s.add, s.del = a, d
		}
		stats[path] = s
	}
	var files []harness.ChangedFile
	tokens = strings.Split(string(nameStatus), "\x00")
	for i := 0; i < len(tokens); {
		code := tokens[i]
		i++
		if code == "" {
			continue
		}
		if i >= len(tokens) || tokens[i] == "" || !strings.ContainsAny(code[:1], "AMDRCTUXB") {
			return nil, errors.New("línea de name-status inesperada")
		}
		if len(files) == maxChangedFiles {
			return nil, fmt.Errorf("%w: más de %d ficheros", errTooManyChanges, maxChangedFiles)
		}
		f := harness.ChangedFile{Status: code[:1], Path: tokens[i]}
		i++
		if f.Status == "R" || f.Status == "C" {
			if i >= len(tokens) || tokens[i] == "" {
				return nil, errors.New("name-status de un renombrado incompleto")
			}
			f.OldPath, f.Path = f.Path, tokens[i]
			i++
		}
		s := stats[f.Path]
		f.Additions, f.Deletions, f.Binary = s.add, s.del, s.binary
		files = append(files, f)
	}
	return files, nil
}

// parseArchive reads the files of a `git archive --format=tar` stream:
// a regular file becomes mode 100755 when executable, else 100644; a
// symbolic link 120000 with its target as data. Anything else at a wanted
// path (a submodule's directory), a wanted path missing (an export-ignore
// attribute) or contents over max bytes make it incomplete. Entries not
// asked for (parent directories, PAX headers) are skipped.
func parseArchive(data []byte, paths []string, max int) (map[string]harness.FileContent, bool, error) {
	want := map[string]bool{}
	for _, p := range paths {
		want[p] = true
	}
	got := map[string]harness.FileContent{}
	complete := true
	total := 0
	tr := tar.NewReader(bytes.NewReader(data))
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, false, err
		}
		name := strings.TrimSuffix(h.Name, "/")
		if !want[name] {
			continue
		}
		if _, dup := got[name]; dup {
			complete = false
			continue
		}
		switch h.Typeflag {
		case tar.TypeReg:
			if h.Size < 0 || int64(total)+h.Size > int64(max) {
				return nil, false, nil
			}
			body, err := io.ReadAll(io.LimitReader(tr, h.Size))
			if err != nil {
				return nil, false, err
			}
			mode := "100644"
			if h.Mode&0o111 != 0 {
				mode = "100755"
			}
			total += len(body)
			got[name] = harness.FileContent{Path: name, Mode: mode, Data: body}
		case tar.TypeSymlink:
			if total+len(h.Linkname) > max {
				return nil, false, nil
			}
			total += len(h.Linkname)
			got[name] = harness.FileContent{Path: name, Mode: "120000", Data: []byte(h.Linkname)}
		default:
			complete = false
			got[name] = harness.FileContent{Path: name}
		}
	}
	for p := range want {
		if _, ok := got[p]; !ok {
			complete = false
		}
	}
	if !complete {
		return nil, false, nil
	}
	return got, true, nil
}
