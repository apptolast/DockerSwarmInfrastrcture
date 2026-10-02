package office

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// File names and bounds of the state directory.
const (
	officeFileName = "office.json"
	jobsFileName   = "jobs.json"
	jobsDirName    = "jobs"
	credDirName    = "credentials"
	codexAuthName  = "codex-auth.json"

	// fileMaxBytes bounds office.json and jobs.json: what is read at
	// start-up is decoded in memory (the pod has 384 MiB), and nothing
	// larger is ever written.
	fileMaxBytes   = 32 << 20
	jobFileMax     = 16 << 20
	maxEventsBytes = 4 << 20
	maxEventLine   = 1 << 20
)

// Job directory files.
const (
	filePrompt       = "prompt.txt"
	fileFinalPrompt  = "final_prompt.txt"
	fileSystemPrompt = "system_prompt.txt"
	fileResult       = "result.md"
	fileEvents       = "events.jsonl"
	filePatch        = "changes.patch"
	fileContents     = "contents.json"
	fileChanges      = "changes.json"
	fileExternal     = "external.json"
)

// officeFile is office.json.
type officeFile struct {
	Schema    int              `json:"schema"`
	Settings  Settings         `json:"settings"`
	Agents    []Agent          `json:"agents"`
	Projects  []Project        `json:"projects"`
	Pipelines []Pipeline       `json:"pipelines"`
	Schedules []Schedule       `json:"schedules"`
	Proposals []Proposal       `json:"proposals"`
	Evals     []Eval           `json:"evals"`
	Seq       map[string]int64 `json:"seq"`
}

// jobsFile is jobs.json.
type jobsFile struct {
	Schema int   `json:"schema"`
	Jobs   []Job `json:"jobs"`
}

// external is untrusted data given to a job (an issue, a PR diff). Note
// is the office's own remark in its place when it could not be fetched
// (no GitHub token for the repository).
type external struct {
	Origin string `json:"origin"`
	Text   string `json:"text"`
	Note   string `json:"note,omitempty"`
}

// contentEntry is one line of contents.json.
type contentEntry struct {
	Path    string `json:"path"`
	Mode    string `json:"mode,omitempty"`
	Deleted bool   `json:"deleted,omitempty"`
	Data    []byte `json:"data,omitempty"`
}

type store struct {
	dir string
	now func() time.Time
}

func openStore(dir string, now func() time.Time) (*store, error) {
	if !filepath.IsAbs(dir) {
		return nil, errors.New("el directorio de estado debe ser absoluto")
	}
	for _, d := range []string{dir, filepath.Join(dir, jobsDirName), filepath.Join(dir, credDirName)} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return nil, fmt.Errorf("creando %s: %w", d, err)
		}
		_ = os.Chmod(d, 0o700)
	}
	return &store{dir: dir, now: now}, nil
}

func (s *store) path(name string) string { return filepath.Join(s.dir, name) }

// writeAtomic replaces path with data: a temporary file in the same
// directory, fsync, rename over, fsync of the directory. With backup the
// current file is kept as path.bak first (hard link, or a copy).
func writeAtomic(path string, data []byte, backup bool) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if backup {
		if err := keepBackup(path); err != nil {
			return err
		}
	}
	if err := os.Rename(name, path); err != nil {
		return err
	}
	return syncDir(dir)
}

// keepBackup makes path.bak a copy of path, never leaving path missing.
func keepBackup(path string) error {
	if _, err := os.Lstat(path); errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	bak, tmp := path+".bak", path+".bak.tmp"
	_ = os.Remove(tmp)
	if err := os.Link(path, tmp); err != nil {
		data, rerr := os.ReadFile(path)
		if rerr != nil {
			return rerr
		}
		if werr := os.WriteFile(tmp, data, 0o600); werr != nil {
			return werr
		}
	}
	return os.Rename(tmp, bak)
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	if err := d.Sync(); err != nil && !errors.Is(err, os.ErrInvalid) {
		return err
	}
	return nil
}

func readBounded(path string, max int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, max+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > max {
		return nil, fmt.Errorf("%s supera %d bytes", filepath.Base(path), max)
	}
	return data, nil
}

// errSchema is an unknown schema: the panel refuses to start rather than
// overwrite data a newer version wrote.
type errSchema struct {
	file   string
	schema int
}

func (e *errSchema) Error() string {
	return fmt.Sprintf("%s tiene el esquema %d, que esta versión no conoce", e.file, e.schema)
}

// loadFile decodes name or, if it is damaged, its .bak. decode must
// reset its target and return the schema it read. found is false when
// the store has neither (a fresh start); warn tells about a recovery.
//
// Only a file that was read whole and is not the JSON it should be (or
// has no schema) counts as damaged and is set aside. Any other failure to
// read it (permissions, an I/O error, a file over fileMaxBytes) stops the
// start-up and renames nothing: the data may be fine and must never be
// replaced by a fresh state.
func (s *store) loadFile(name string, decode func([]byte) (int, error)) (found bool, warn string, err error) {
	main, bak := s.path(name), s.path(name)+".bak"
	try := func(path string) (bool, bool, error) { // exists, ok, fatal
		data, err := readBounded(path, fileMaxBytes)
		if errors.Is(err, fs.ErrNotExist) {
			return false, false, nil
		}
		if err != nil {
			return true, false, fmt.Errorf("no se pudo leer %s (no se toca nada): %w", filepath.Base(path), err)
		}
		schema, derr := decode(data)
		if derr != nil || schema == 0 {
			return true, false, nil
		}
		if schema != SchemaVersion {
			return true, false, &errSchema{file: filepath.Base(path), schema: schema}
		}
		return true, true, nil
	}
	mainExists, ok, fatal := try(main)
	if fatal != nil {
		return false, "", fatal
	}
	if ok {
		return true, "", nil
	}
	bakExists, ok, fatal := try(bak)
	if fatal != nil {
		return false, "", fatal
	}
	stamp := strconv.FormatInt(s.now().Unix(), 10)
	aside := func(path string) error {
		if err := os.Rename(path, path+".corrupt-"+stamp); err != nil {
			return fmt.Errorf("no se pudo apartar %s dañado: %w", filepath.Base(path), err)
		}
		return nil
	}
	if ok {
		if mainExists {
			if err := aside(main); err != nil {
				return false, "", err
			}
			return true, fmt.Sprintf("%s estaba dañado: se recuperó la copia anterior (%s.bak); el dañado quedó como %s.corrupt-%s.",
				name, name, name, stamp), nil
		}
		return true, fmt.Sprintf("Faltaba %s: se recuperó la copia anterior (%s.bak).", name, name), nil
	}
	if !mainExists && !bakExists {
		return false, "", nil
	}
	if mainExists {
		if err := aside(main); err != nil {
			return false, "", err
		}
	}
	if bakExists {
		if err := aside(bak); err != nil {
			return false, "", err
		}
	}
	return false, fmt.Sprintf("%s y su copia estaban dañados: se apartaron como *.corrupt-%s y se empezó de cero.",
		name, stamp), nil
}

func (s *store) loadOffice() (*officeFile, bool, string, error) {
	var of officeFile
	found, warn, err := s.loadFile(officeFileName, func(data []byte) (int, error) {
		of = officeFile{}
		err := json.Unmarshal(data, &of)
		return of.Schema, err
	})
	if err != nil || !found {
		return nil, false, warn, err
	}
	return &of, true, warn, nil
}

func (s *store) loadJobs() ([]Job, string, error) {
	var jf jobsFile
	found, warn, err := s.loadFile(jobsFileName, func(data []byte) (int, error) {
		jf = jobsFile{}
		err := json.Unmarshal(data, &jf)
		return jf.Schema, err
	})
	if err != nil || !found {
		return nil, warn, err
	}
	return jf.Jobs, warn, nil
}

// errTooBig refuses to write a state file the next start-up could not
// read (over fileMaxBytes); the file on disk stays as it was.
var errTooBig = fmt.Errorf("supera %d MiB", fileMaxBytes>>20)

func (s *store) saveOffice(of *officeFile) error {
	of.Schema = SchemaVersion
	data, err := json.MarshalIndent(of, "", " ")
	if err != nil {
		return err
	}
	if len(data) > fileMaxBytes {
		return errTooBig
	}
	return writeAtomic(s.path(officeFileName), data, true)
}

func (s *store) saveJobs(jobs []Job) error {
	data, err := json.Marshal(jobsFile{Schema: SchemaVersion, Jobs: jobs})
	if err != nil {
		return err
	}
	if len(data) > fileMaxBytes {
		return errTooBig
	}
	return writeAtomic(s.path(jobsFileName), data, true)
}

func (s *store) jobDir(id string) string { return filepath.Join(s.dir, jobsDirName, id) }

func (s *store) writeJobFile(id, name string, data []byte) error {
	if !ValidJobID(id) {
		return errors.New("identificador de trabajo no válido")
	}
	dir := s.jobDir(id)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return writeAtomic(filepath.Join(dir, name), data, false)
}

func (s *store) writeJobJSON(id, name string, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return s.writeJobFile(id, name, data)
}

// readJobFile returns a job file, or nil when it does not exist.
func (s *store) readJobFile(id, name string, max int64) ([]byte, error) {
	if !ValidJobID(id) {
		return nil, errors.New("identificador de trabajo no válido")
	}
	data, err := readBounded(filepath.Join(s.jobDir(id), name), max)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	return data, err
}

func (s *store) readJobJSON(id, name string, max int64, v any) (bool, error) {
	data, err := s.readJobFile(id, name, max)
	if err != nil || data == nil {
		return false, err
	}
	return true, json.Unmarshal(data, v)
}

func (s *store) removeJob(id string) error {
	if !ValidJobID(id) {
		return errors.New("identificador de trabajo no válido")
	}
	return os.RemoveAll(s.jobDir(id))
}

// removeJobFile deletes one file of a job's directory, if it exists.
func (s *store) removeJobFile(id, name string) error {
	if !ValidJobID(id) {
		return errors.New("identificador de trabajo no válido")
	}
	err := os.Remove(filepath.Join(s.jobDir(id), name))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

// jobDirSize is the bytes of a job's directory (its files are flat).
func (s *store) jobDirSize(id string) int64 {
	if !ValidJobID(id) {
		return 0
	}
	entries, err := os.ReadDir(s.jobDir(id))
	if err != nil {
		return 0
	}
	var n int64
	for _, e := range entries {
		if info, err := e.Info(); err == nil && info.Mode().IsRegular() {
			n += info.Size()
		}
	}
	return n
}

// eventLog appends a job's events to events.jsonl, at most
// maxEventsBytes, then one "registro truncado" line.
type eventLog struct {
	f         *os.File
	size      int64
	truncated bool
}

func (s *store) openEventLog(id string) (*eventLog, error) {
	if !ValidJobID(id) {
		return nil, errors.New("identificador de trabajo no válido")
	}
	dir := s.jobDir(id)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, fileEvents), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	l := &eventLog{f: f, size: info.Size(), truncated: info.Size() >= maxEventsBytes}
	if !l.truncated && l.size > maxEventsBytes/2 {
		// A log cut earlier ends with the marker line.
		tail := make([]byte, min(l.size, 512))
		if r, err := os.Open(filepath.Join(dir, fileEvents)); err == nil {
			_, _ = r.ReadAt(tail, l.size-int64(len(tail)))
			r.Close()
			l.truncated = bytes.Contains(tail, []byte(`"registro truncado"`))
		}
	}
	return l, nil
}

func (l *eventLog) append(ev harness.Event) {
	if l == nil || l.f == nil || l.truncated {
		return
	}
	data, err := json.Marshal(ev)
	if err != nil {
		return
	}
	data = append(data, '\n')
	if l.size+int64(len(data)) > maxEventsBytes {
		marker, _ := json.Marshal(harness.Event{Seq: ev.Seq, Time: ev.Time, Kind: harness.EventSystem, Text: "registro truncado"})
		_, _ = l.f.Write(append(marker, '\n'))
		l.truncated = true
		return
	}
	n, _ := l.f.Write(data)
	l.size += int64(n)
}

func (l *eventLog) close() {
	if l != nil && l.f != nil {
		_ = l.f.Sync()
		_ = l.f.Close()
		l.f = nil
	}
}

// readEvents reads a job's stored events; damaged lines are skipped.
func (s *store) readEvents(id string) ([]harness.Event, error) {
	if !ValidJobID(id) {
		return nil, errors.New("identificador de trabajo no válido")
	}
	f, err := os.Open(filepath.Join(s.jobDir(id), fileEvents))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []harness.Event
	sc := bufio.NewScanner(io.LimitReader(f, maxEventsBytes+maxEventLine))
	sc.Buffer(make([]byte, 64<<10), maxEventLine)
	for sc.Scan() {
		var ev harness.Event
		if json.Unmarshal(bytes.TrimSpace(sc.Bytes()), &ev) == nil && ev.Kind != "" {
			out = append(out, ev)
		}
	}
	return out, nil
}

// codexAuthPath is the stored, possibly renewed, Codex auth.json.
func (s *store) codexAuthPath() string { return filepath.Join(s.dir, credDirName, codexAuthName) }
