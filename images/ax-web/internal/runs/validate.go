package runs

import (
	"net/url"
	"regexp"
	"strings"
)

// Repository and branch rules (they extend ax-tarea's https-only
// repository). The office validates its projects and jobs with them, and
// Launch checks them again.
const (
	MaxRepoLength   = 300
	DefaultBranch   = "main"
	maxBranchLength = 100
)

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

// checkRepo re-checks the shape of an already validated repository URL:
// the allowed hosts are the office's to enforce, the shape is the run
// manager's.
func checkRepo(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return &FieldError{"repo", "el repositorio no es una URL válida"}
	}
	_, err = ValidateRepo(raw, []string{u.Host})
	return err
}

func oneOf(s string, set []string) bool {
	for _, v := range set {
		if s == v {
			return true
		}
	}
	return false
}
