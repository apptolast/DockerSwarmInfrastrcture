package runs

import (
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// Input limits (the rules extend ax-tarea's https-only repository and
// integer turn count).
const (
	MaxRepoLength   = 300
	MaxPromptBytes  = 16 << 10
	MinTimeout      = 5 * time.Minute
	DefaultTurns    = 20
	DefaultTimeout  = 30 * time.Minute
	DefaultBranch   = "main"
	DefaultCPU      = "1"
	DefaultMemory   = "1Gi"
	RequestCPU      = "250m"
	RequestMemory   = "512Mi"
	AgentClaude     = "claude"
	maxBranchLength = 100
)

// Resource choices, recorded on the task only: AX does not enforce them
// (google/ax#369); the worker pods and the timeout are the real caps.
var (
	CPUChoices    = []string{"500m", "1", "2"}
	MemoryChoices = []string{"512Mi", "1Gi", "1536Mi"}
)

// Request is the browser's JSON body for a new run.
type Request struct {
	Repo           string `json:"repo"`
	Branch         string `json:"branch"`
	Prompt         string `json:"prompt"`
	Agent          string `json:"agent"`
	Turns          *int   `json:"turns"`
	TimeoutMinutes *int   `json:"timeout_minutes"`
	CPU            string `json:"cpu"`
	Memory         string `json:"memory"`
}

// Spec is a validated run.
type Spec struct {
	Repo    string
	Branch  string
	Prompt  string
	Turns   int
	Timeout time.Duration
	CPU     string
	Memory  string
}

// Limits come from the reviewed configuration.
type Limits struct {
	RepoHosts   []string
	MaxTurns    int
	MaxTimeout  time.Duration
	PromptInArg bool
}

// FieldError names the rejected field; Message is shown to the owner.
type FieldError struct {
	Field   string
	Message string
}

func (e *FieldError) Error() string { return e.Field + ": " + e.Message }

var (
	pathSegment = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)
	branchChars = regexp.MustCompile(`^[A-Za-z0-9._/-]{1,100}$`)
)

// Validate checks every field and fills the defaults.
func Validate(req Request, lim Limits) (Spec, error) {
	repo, err := ValidateRepo(req.Repo, lim.RepoHosts)
	if err != nil {
		return Spec{}, err
	}
	branch := req.Branch
	if branch == "" {
		branch = DefaultBranch
	}
	if err := ValidateBranch(branch); err != nil {
		return Spec{}, err
	}
	if req.Agent != "" && req.Agent != AgentClaude {
		return Spec{}, &FieldError{"agent", "solo Claude está disponible"}
	}
	prompt := req.Prompt
	switch {
	case strings.TrimSpace(prompt) == "":
		return Spec{}, &FieldError{"prompt", "la instrucción está vacía"}
	case len(prompt) > MaxPromptBytes:
		return Spec{}, &FieldError{"prompt", "la instrucción supera 16 KiB"}
	case !utf8.ValidString(prompt) || strings.ContainsRune(prompt, 0):
		return Spec{}, &FieldError{"prompt", "la instrucción no es texto UTF-8 válido"}
	case lim.PromptInArg && strings.HasPrefix(prompt, "-"):
		return Spec{}, &FieldError{"prompt", "la instrucción no puede empezar por «-»"}
	}
	turns := DefaultTurns
	if req.Turns != nil {
		turns = *req.Turns
	}
	if turns < 1 || turns > lim.MaxTurns {
		return Spec{}, &FieldError{"turns", fmt.Sprintf("los turnos van de 1 a %d", lim.MaxTurns)}
	}
	timeout := DefaultTimeout
	if timeout > lim.MaxTimeout {
		timeout = lim.MaxTimeout
	}
	if req.TimeoutMinutes != nil {
		timeout = time.Duration(*req.TimeoutMinutes) * time.Minute
	}
	if timeout < MinTimeout || timeout > lim.MaxTimeout {
		return Spec{}, &FieldError{"timeout_minutes", fmt.Sprintf(
			"el tiempo máximo va de %d a %d minutos",
			int(MinTimeout.Minutes()), int(lim.MaxTimeout.Minutes()))}
	}
	cpu := req.CPU
	if cpu == "" {
		cpu = DefaultCPU
	}
	if !oneOf(cpu, CPUChoices) {
		return Spec{}, &FieldError{"cpu", "CPU no admitida"}
	}
	memory := req.Memory
	if memory == "" {
		memory = DefaultMemory
	}
	if !oneOf(memory, MemoryChoices) {
		return Spec{}, &FieldError{"memory", "memoria no admitida"}
	}
	return Spec{
		Repo: repo, Branch: branch, Prompt: prompt, Turns: turns,
		Timeout: timeout, CPU: cpu, Memory: memory,
	}, nil
}

// ValidateRepo accepts only https://<allowed host>/<owner>/<name>[.git],
// with no userinfo, port, query, fragment, escapes or dot segments, and
// returns it in canonical form. AX itself does not validate the URL
// (google/ax#363) and the workspace has no git credential, so only
// public repositories work.
func ValidateRepo(raw string, hosts []string) (string, error) {
	bad := func(msg string) (string, error) { return "", &FieldError{"repo", msg} }
	if raw == "" {
		return bad("falta el repositorio")
	}
	if len(raw) > MaxRepoLength {
		return bad("el repositorio supera 300 caracteres")
	}
	for _, r := range raw {
		if r <= ' ' || r >= 0x7f || r == '%' || r == '\\' {
			return bad("el repositorio tiene caracteres no admitidos")
		}
	}
	u, err := url.Parse(raw)
	if err != nil {
		return bad("el repositorio no es una URL válida")
	}
	if u.Scheme != "https" {
		return bad("solo repositorios https://")
	}
	if u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" ||
		strings.Contains(raw, "#") || u.Opaque != "" || u.Port() != "" {
		return bad("la URL no puede llevar usuario, puerto, consulta ni fragmento")
	}
	if !oneOf(u.Host, hosts) {
		return bad("el host del repositorio no está permitido")
	}
	parts := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if !strings.HasPrefix(u.Path, "/") || len(parts) != 2 {
		return bad("la ruta debe ser /<propietario>/<repositorio>")
	}
	for _, p := range parts {
		if !pathSegment.MatchString(p) || strings.Contains(p, "..") ||
			strings.HasPrefix(p, "-") || strings.HasPrefix(p, ".") || strings.HasSuffix(p, ".") {
			return bad("la ruta debe ser /<propietario>/<repositorio>")
		}
	}
	return "https://" + u.Host + "/" + parts[0] + "/" + parts[1], nil
}

// ValidateBranch follows the safe subset of git check-ref-format.
func ValidateBranch(b string) error {
	bad := &FieldError{"branch", "rama no válida"}
	if len(b) > maxBranchLength || !branchChars.MatchString(b) ||
		strings.HasPrefix(b, "-") || strings.HasPrefix(b, "/") ||
		strings.HasSuffix(b, "/") || strings.HasSuffix(b, ".") ||
		strings.HasSuffix(b, ".lock") || strings.Contains(b, "..") ||
		strings.Contains(b, "//") || strings.Contains(b, "/.") ||
		strings.HasPrefix(b, ".") {
		return bad
	}
	return nil
}

func oneOf(s string, set []string) bool {
	for _, v := range set {
		if s == v {
			return true
		}
	}
	return false
}
