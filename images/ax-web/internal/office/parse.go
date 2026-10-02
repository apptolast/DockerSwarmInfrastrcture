package office

import (
	"encoding/json"
	"errors"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"apptolast.com/ax-web/internal/harness"
)

// Bounds of what is parsed out of a result.
const (
	MaxSummaryRunes   = 2000
	fallbackSummary   = 300
	MaxLessons        = 3
	MaxLessonRunes    = 400
	maxCoachPromptLen = 16 << 10
)

var (
	summaryHead = regexp.MustCompile(`(?im)^#{1,3}[ \t]*resumen[ \t]*:?[ \t]*$`)
	lessonsHead = regexp.MustCompile(`(?im)^#{1,3}[ \t]*lecciones[ \t]*:?[ \t]*$`)
	nextHead    = regexp.MustCompile(`(?m)^#{1,2}[ \t]`)
	bullet      = regexp.MustCompile(`^(?:[-*•]|\d{1,2}[.)])[ \t]+(.*)$`)
	verdictRe   = regexp.MustCompile(`(?im)^\W*VEREDICTO\W*:\W*(APROBADO|CAMBIOS)`)
	scoreRe     = regexp.MustCompile(`(?i)PUNTUACI[OÓ]N\W*:\W*(\d{1,2})`)
	jsonFence   = regexp.MustCompile("(?s)```json[ \\t]*\\r?\\n(.*?)\\r?\\n[ \\t]*```")
)

// section is the text after the last heading matched by head, up to the
// next "#"/"##" heading.
func section(result string, head *regexp.Regexp) (string, bool) {
	locs := head.FindAllStringIndex(result, -1)
	if len(locs) == 0 {
		return "", false
	}
	rest := result[locs[len(locs)-1][1]:]
	if next := nextHead.FindStringIndex(rest); next != nil {
		rest = rest[:next[0]]
	}
	return strings.TrimSpace(rest), true
}

func truncRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return strings.TrimSpace(string(r[:n])) + "…"
}

// ParseSummary is the "## Resumen" section, or the start of the result.
func ParseSummary(result string) string {
	if s, ok := section(result, summaryHead); ok && s != "" {
		return truncRunes(s, MaxSummaryRunes)
	}
	return truncRunes(strings.TrimSpace(result), fallbackSummary)
}

// ParseLessons are the bullets of the last "## Lecciones" section.
func ParseLessons(result string) []string {
	s, ok := section(result, lessonsHead)
	if !ok {
		return nil
	}
	var out []string
	for l := range strings.Lines(s) {
		m := bullet.FindStringSubmatch(strings.TrimSpace(l))
		if m == nil {
			continue
		}
		item := strings.TrimSpace(m[1])
		norm := strings.Trim(strings.ToLower(item), " .«»\"'()")
		if item == "" || norm == "ninguna" || norm == "ninguno" || norm == "n/a" || norm == "ninguna lección" {
			continue
		}
		out = append(out, truncRunes(item, MaxLessonRunes))
		if len(out) == MaxLessons {
			break
		}
	}
	return out
}

// ParseVerdict is the last "VEREDICTO: APROBADO|CAMBIOS", or "".
func ParseVerdict(result string) string {
	all := verdictRe.FindAllStringSubmatch(result, -1)
	if len(all) == 0 {
		return ""
	}
	if strings.EqualFold(all[len(all)-1][1], "APROBADO") {
		return VerdictApproved
	}
	return VerdictChanges
}

// ParseScore is the last "PUNTUACION: n", clamped to 0-10.
func ParseScore(result string) *int {
	all := scoreRe.FindAllStringSubmatch(result, -1)
	if len(all) == 0 {
		return nil
	}
	n, err := strconv.Atoi(all[len(all)-1][1])
	if err != nil {
		return nil
	}
	n = max(0, min(10, n))
	return &n
}

// CoachProposal is the JSON block a coach job answers with.
type CoachProposal struct {
	SystemPrompt string   `json:"system_prompt"`
	Motivo       string   `json:"motivo"`
	Cambios      []string `json:"cambios"`
}

// ParseCoach reads the last ```json block of a coach result.
func ParseCoach(result string) (CoachProposal, error) {
	all := jsonFence.FindAllStringSubmatch(result, -1)
	if len(all) == 0 {
		return CoachProposal{}, errors.New("la respuesta del Coach no trae el bloque JSON")
	}
	var p CoachProposal
	if err := json.Unmarshal([]byte(all[len(all)-1][1]), &p); err != nil {
		return CoachProposal{}, errors.New("el bloque JSON del Coach no es válido")
	}
	p.SystemPrompt = strings.TrimSpace(p.SystemPrompt)
	if p.SystemPrompt == "" || len(p.SystemPrompt) > maxCoachPromptLen ||
		!utf8.ValidString(p.SystemPrompt) || strings.ContainsRune(p.SystemPrompt, 0) {
		return CoachProposal{}, errors.New("el prompt que propone el Coach está vacío o supera 16 KiB")
	}
	p.Motivo = truncRunes(strings.TrimSpace(p.Motivo), 2000)
	if len(p.Cambios) > 20 {
		p.Cambios = p.Cambios[:20]
	}
	for i := range p.Cambios {
		p.Cambios[i] = truncRunes(strings.TrimSpace(p.Cambios[i]), 400)
	}
	return p, nil
}

// statusOf maps a run's result to a job status.
func statusOf(r harness.Result) string {
	switch {
	case r.Outcome == harness.OutcomeCancelled:
		return StatusCancelled
	case r.Outcome == harness.OutcomeExited && r.ExitCode != nil && *r.ExitCode == 0 && !r.IsError:
		return StatusDone
	}
	return StatusFailed
}
