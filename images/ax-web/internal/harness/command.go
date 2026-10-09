package harness

import (
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Wrapper is the ax-agents image's entry point for both CLIs: it installs
// the credential from the environment and runs the CLI unmodified.
const Wrapper = "ax-agent"

// Bounds of a Spec, enforced by Validate.
const (
	MaxTurns             = 500
	MinTimeout           = time.Minute
	MaxTimeout           = 3 * time.Hour
	MaxPromptBytes       = 256 << 10
	MaxSystemPromptBytes = 32 << 10
	MaxDisallowedTools   = 20
)

// Efforts each CLI accepts (Claude Code 2.1.274 --effort; Codex 0.156.1
// model_reasoning_effort, from `codex debug models`).
var (
	ClaudeEfforts = []string{"low", "medium", "high", "xhigh", "max"}
	CodexEfforts  = []string{"low", "medium", "high", "xhigh", "max", "ultra"}
)

var (
	idPattern    = regexp.MustCompile(`^web-[a-z0-9][a-z0-9-]{0,58}$`)
	modelPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$`)
	toolPattern  = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_*():. -]{0,80}$`)
)

// SpecError names the field of a Spec that Validate rejected. The message
// is Spanish, for whoever built the Spec.
type SpecError struct {
	Field   string
	Message string
}

func (e *SpecError) Error() string { return e.Field + ": " + e.Message }

func specErr(field, format string, args ...any) error {
	return &SpecError{Field: field, Message: fmt.Sprintf(format, args...)}
}

// Validate checks what the run manager needs of a Spec before anything of
// it reaches an argv: names and values from fixed sets or narrow patterns,
// bounded texts, no NUL anywhere and no newline in an argv value except
// the persona. Repo and Branch are the office's to validate (runs
// re-checks them).
func Validate(s Spec) error {
	if !idPattern.MatchString(s.ID) {
		return specErr("id", "identificador de ejecución no válido")
	}
	var efforts []string
	switch s.Harness {
	case Claude:
		efforts = ClaudeEfforts
	case Codex:
		efforts = CodexEfforts
	default:
		return specErr("harness", "herramienta de agente desconocida")
	}
	if s.Mode != ModeRead && s.Mode != ModeFull {
		return specErr("mode", "modo desconocido")
	}
	if s.Model != "" && !modelPattern.MatchString(s.Model) {
		return specErr("model", "modelo no válido")
	}
	if s.FallbackModel != "" && !modelPattern.MatchString(s.FallbackModel) {
		return specErr("fallback_model", "modelo de respaldo no válido")
	}
	if s.Advisor != "" {
		if s.Harness != Claude {
			return specErr("advisor", "solo Claude admite un consejero")
		}
		if !modelPattern.MatchString(s.Advisor) || s.Advisor == s.Model {
			return specErr("advisor", "consejero no válido")
		}
	}
	if s.Effort != "" && !slices.Contains(efforts, s.Effort) {
		return specErr("effort", "esfuerzo no admitido por %s", s.Harness)
	}
	if s.Harness == Claude && (s.MaxTurns < 1 || s.MaxTurns > MaxTurns) {
		return specErr("max_turns", "los turnos van de 1 a %d", MaxTurns)
	}
	if s.Timeout < MinTimeout || s.Timeout > MaxTimeout {
		return specErr("timeout", "el tiempo máximo va de 1 minuto a 3 horas")
	}
	switch {
	case strings.TrimSpace(s.Prompt) == "":
		return specErr("prompt", "la instrucción está vacía")
	case len(s.Prompt) > MaxPromptBytes:
		return specErr("prompt", "la instrucción supera 256 KiB")
	case strings.ContainsRune(s.Prompt, 0) || !utf8.ValidString(s.Prompt):
		return specErr("prompt", "la instrucción no es texto UTF-8 válido")
	}
	switch {
	case len(s.AppendSystemPrompt) > MaxSystemPromptBytes:
		return specErr("system_prompt", "el prompt de sistema supera 32 KiB")
	case strings.ContainsRune(s.AppendSystemPrompt, 0) || !utf8.ValidString(s.AppendSystemPrompt):
		return specErr("system_prompt", "el prompt de sistema no es texto UTF-8 válido")
	}
	if len(s.DisallowedTools) > MaxDisallowedTools {
		return specErr("disallowed_tools", "como mucho %d herramientas prohibidas", MaxDisallowedTools)
	}
	for _, t := range s.DisallowedTools {
		if !toolPattern.MatchString(t) {
			return specErr("disallowed_tools", "herramienta prohibida no válida")
		}
	}
	return nil
}

// Command is the agent argv for s, after Validate. The prompt is never in
// it: it goes to stdin.
//
// Claude: read mode is --restricted (no tools that run commands or code,
// no WebFetch, no user/project/local settings, file tools confined to the
// working directory); full mode loads only the image's user settings, so
// a repository's .claude hooks never run, and bypasses permission prompts
// inside the gVisor sandbox (which needs IS_SANDBOX=1 as root, see
// ExtraEnv). --strict-mcp-config always skips the repository's MCP
// servers. stream-json, which needs --verbose with -p, is the only
// realtime output format.
//
// Codex: JSONL events, ephemeral session, the sandbox mode the image's
// wrapper configures anyway (Codex's own bubblewrap sandbox cannot run
// under gVisor), and "-" to read the prompt from stdin.
func Command(s Spec) ([]string, error) {
	if err := Validate(s); err != nil {
		return nil, err
	}
	if s.Harness == Codex {
		argv := []string{Wrapper, Codex, "exec", "--json", "--color", "never", "--ephemeral",
			"--sandbox", "danger-full-access"}
		if s.Model != "" {
			argv = append(argv, "-m", s.Model)
		}
		if s.Effort != "" {
			quoted, _ := json.Marshal(s.Effort)
			argv = append(argv, "-c", "model_reasoning_effort="+string(quoted))
		}
		return append(argv, "-"), nil
	}
	argv := []string{Wrapper, Claude, "-p", "--output-format", "stream-json", "--verbose",
		"--strict-mcp-config", "--max-turns", strconv.Itoa(s.MaxTurns)}
	if s.Mode == ModeRead {
		argv = append(argv, "--restricted")
	} else {
		argv = append(argv, "--setting-sources", "user", "--permission-mode", "bypassPermissions")
		if len(s.DisallowedTools) > 0 {
			argv = append(argv, "--disallowed-tools", strings.Join(s.DisallowedTools, ","))
		}
	}
	for _, f := range []struct{ flag, value string }{
		{"--model", s.Model}, {"--fallback-model", s.FallbackModel},
		{"--effort", s.Effort}, {"--append-system-prompt", s.AppendSystemPrompt},
		{"--advisor", s.Advisor},
	} {
		if f.value != "" {
			argv = append(argv, f.flag, f.value)
		}
	}
	return argv, nil
}

// ExtraEnv is the environment the command needs besides the credential:
// Claude Code refuses bypassPermissions as root unless IS_SANDBOX=1, and
// the sandbox runs as uid 0.
func ExtraEnv(s Spec) map[string]string {
	if s.Harness == Claude && s.Mode == ModeFull {
		return map[string]string{"IS_SANDBOX": "1"}
	}
	return nil
}
