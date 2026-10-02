package office

import (
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"

	"apptolast.com/ax-web/internal/harness"
	"apptolast.com/ax-web/internal/runs"
)

// Bounds of the office's own records.
const (
	MaxNameRunes         = 60
	MaxEmojiBytes        = 16
	MaxSystemPromptBytes = 16 << 10
	MaxJobPromptBytes    = 32 << 10
	MaxTitleRunes        = 120
	MaxMemoryRunes       = 600
	MaxMemoryItems       = 200
	MaxNotesBytes        = 8 << 10
	MaxDescriptionRunes  = 300
	MaxNoteRunes         = 1000
	MaxDisallowedTools   = harness.MaxDisallowedTools
	MaxCriteriaBytes     = 8 << 10
	MinTimeoutMinutes    = 5
	MaxIterationsLimit   = 10
	MaxLessonsLimit      = 200
)

var (
	idRe      = regexp.MustCompile(`^[a-z][a-z0-9-]{1,31}$`)
	colorRe   = regexp.MustCompile(`^#[0-9a-fA-F]{6}$`)
	modelRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$`)
	toolRe    = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_*():. -]{0,80}$`)
	timeRe    = regexp.MustCompile(`^([01][0-9]|2[0-3]):[0-5][0-9]$`)
	serviceRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]{0,79}$`)
	jobIDRe   = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,58}$`)
)

// ValidID reports whether s is a valid office identifier.
func ValidID(s string) bool { return idRe.MatchString(s) }

// ValidJobID reports whether s can be a job id (it becomes "web-" + id,
// an AX task name).
func ValidJobID(s string) bool { return jobIDRe.MatchString(s) }

var accents = strings.NewReplacer(
	"á", "a", "à", "a", "â", "a", "ä", "a", "ã", "a",
	"é", "e", "è", "e", "ê", "e", "ë", "e",
	"í", "i", "ì", "i", "î", "i", "ï", "i",
	"ó", "o", "ò", "o", "ô", "o", "ö", "o", "õ", "o",
	"ú", "u", "ù", "u", "û", "u", "ü", "u",
	"ñ", "n", "ç", "c",
)

// slug derives an identifier from a name: lower case, accents stripped,
// anything else than [a-z0-9] turned into "-", trimmed to 32 characters.
func slug(name, fallback string) string {
	s := accents.Replace(strings.ToLower(name))
	var b strings.Builder
	dash := false
	for _, r := range s {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			dash = false
		} else if !dash && b.Len() > 0 {
			b.WriteByte('-')
			dash = true
		}
	}
	out := strings.Trim(b.String(), "-")
	if out == "" || out[0] < 'a' || out[0] > 'z' {
		out = strings.Trim(fallback+"-"+out, "-")
	}
	if len(out) < 2 {
		out = fallback
	}
	if len(out) > 28 {
		out = strings.TrimRight(out[:28], "-")
	}
	return out
}

// uniqueID returns base, or base-2, base-3... the first not taken.
func uniqueID(base string, taken func(string) bool) string {
	if !taken(base) {
		return base
	}
	for i := 2; ; i++ {
		c := fmt.Sprintf("%s-%d", base, i)
		if !taken(c) {
			return c
		}
	}
}

// line checks a one-line text: valid UTF-8, no control characters, at
// most max runes; empty is refused when required.
func line(field, s string, max int, required bool) (string, error) {
	s = strings.TrimSpace(s)
	if required && s == "" {
		return "", fieldErr(field, "es obligatorio")
	}
	if !utf8.ValidString(s) {
		return "", fieldErr(field, "no es texto UTF-8 válido")
	}
	for _, r := range s {
		if unicode.IsControl(r) {
			return "", fieldErr(field, "no puede tener saltos de línea ni caracteres de control")
		}
	}
	if utf8.RuneCountInString(s) > max {
		return "", fieldErr(field, fmt.Sprintf("supera %d caracteres", max))
	}
	return s, nil
}

// text checks a multi-line text bounded in bytes.
func text(field, s string, maxBytes int, required bool) (string, error) {
	if required && strings.TrimSpace(s) == "" {
		return "", fieldErr(field, "está vacío")
	}
	if !utf8.ValidString(s) || strings.ContainsRune(s, 0) {
		return "", fieldErr(field, "no es texto UTF-8 válido")
	}
	if len(s) > maxBytes {
		return "", fieldErr(field, "supera "+sizeLabel(maxBytes))
	}
	return s, nil
}

func sizeLabel(n int) string {
	if n%1024 == 0 {
		return fmt.Sprintf("%d KiB", n/1024)
	}
	return fmt.Sprintf("%d bytes", n)
}

// runsError turns a runs validation error into a FieldError of field.
func runsError(field string, err error) error {
	msg := err.Error()
	if i := strings.Index(msg, ": "); i >= 0 {
		msg = msg[i+2:]
	}
	return fieldErr(field, msg)
}

// validateBehaviour checks the fields that shape a run. prefix names the
// fields in errors ("" or "overrides.").
func validateBehaviour(prefix string, b *AgentSnapshot, lim Limits, disallowed []string) error {
	if b.Harness != harness.Claude && b.Harness != harness.Codex {
		return fieldErr(prefix+"harness", "el agente debe ser claude o codex")
	}
	if b.Mode != harness.ModeRead && b.Mode != harness.ModeFull {
		return fieldErr(prefix+"mode", "el modo debe ser lectura o completo")
	}
	if b.Model != "" && !modelRe.MatchString(b.Model) {
		return fieldErr(prefix+"model", "modelo no válido")
	}
	if b.FallbackModel != "" {
		if b.Harness != harness.Claude {
			return fieldErr(prefix+"fallback_model", "solo Claude admite un modelo de reserva")
		}
		if !modelRe.MatchString(b.FallbackModel) {
			return fieldErr(prefix+"fallback_model", "modelo de reserva no válido")
		}
	}
	if b.Effort != "" && !slices.Contains(effortsFor(b.Harness, b.Model), b.Effort) {
		return fieldErr(prefix+"effort", "ese esfuerzo no está disponible para el modelo elegido")
	}
	minTurns := 1
	if b.Harness == harness.Codex {
		minTurns = 0
	}
	if b.MaxTurns < minTurns || b.MaxTurns > lim.MaxTurns {
		return fieldErr(prefix+"max_turns", fmt.Sprintf("los turnos van de %d a %d", minTurns, lim.MaxTurns))
	}
	if b.TimeoutMinutes < MinTimeoutMinutes || b.TimeoutMinutes > lim.MaxTimeoutMinutes {
		return fieldErr(prefix+"timeout_minutes", fmt.Sprintf("el tiempo máximo va de %d a %d minutos",
			MinTimeoutMinutes, lim.MaxTimeoutMinutes))
	}
	if len(disallowed) > MaxDisallowedTools {
		return fieldErr("disallowed_tools", fmt.Sprintf("como mucho %d herramientas", MaxDisallowedTools))
	}
	for _, t := range disallowed {
		if !toolRe.MatchString(t) {
			return fieldErr("disallowed_tools", "nombre de herramienta no válido")
		}
	}
	return nil
}

// validateAgent checks and normalizes every field of a.
func validateAgent(a *Agent, lim Limits) error {
	var err error
	if a.Name, err = line("name", a.Name, MaxNameRunes, true); err != nil {
		return err
	}
	if a.Role, err = line("role", a.Role, MaxNameRunes, false); err != nil {
		return err
	}
	a.Emoji = strings.TrimSpace(a.Emoji)
	if a.Emoji == "" {
		a.Emoji = "🤖"
	}
	if len(a.Emoji) > MaxEmojiBytes || !utf8.ValidString(a.Emoji) || strings.ContainsAny(a.Emoji, "\x00\n\r<>") {
		return fieldErr("emoji", "el emoji admite como mucho 16 bytes")
	}
	if a.Color == "" {
		a.Color = "#64748b"
	}
	if !colorRe.MatchString(a.Color) {
		return fieldErr("color", "el color debe ser #rrggbb")
	}
	if a.SystemPrompt, err = text("system_prompt", a.SystemPrompt, lim.MaxSystemPrompt, false); err != nil {
		return err
	}
	if a.DisallowedTools == nil {
		a.DisallowedTools = []string{}
	}
	b := behaviourOf(a)
	return validateBehaviour("", &b, lim, a.DisallowedTools)
}

func behaviourOf(a *Agent) AgentSnapshot {
	return AgentSnapshot{
		Name: a.Name, Emoji: a.Emoji, Version: a.Version, Harness: a.Harness, Model: a.Model,
		FallbackModel: a.FallbackModel, Effort: a.Effort, Mode: a.Mode,
		MaxTurns: a.MaxTurns, TimeoutMinutes: a.TimeoutMinutes,
	}
}

// effective applies o to the agent's settings. A different harness
// starts from that harness's defaults: the agent's model belongs to the
// other CLI.
func effective(a *Agent, o *Overrides) AgentSnapshot {
	b := behaviourOf(a)
	if o == nil {
		return b
	}
	if o.Harness != "" && o.Harness != b.Harness {
		b.Harness, b.Model, b.FallbackModel, b.Effort = o.Harness, "", "", ""
		if o.Harness == harness.Codex {
			b.MaxTurns = 0
		} else if b.MaxTurns == 0 {
			b.MaxTurns = 40
		}
	}
	if o.Model != "" {
		b.Model = o.Model
		if o.Effort == "" && b.Effort != "" && !slices.Contains(effortsFor(b.Harness, b.Model), b.Effort) {
			b.Effort = ""
		}
	}
	if o.FallbackModel != "" {
		b.FallbackModel = o.FallbackModel
	}
	if o.Effort != "" {
		b.Effort = o.Effort
	}
	if o.Mode != "" {
		b.Mode = o.Mode
	}
	if o.MaxTurns != 0 {
		b.MaxTurns = o.MaxTurns
	}
	if o.TimeoutMinutes != 0 {
		b.TimeoutMinutes = o.TimeoutMinutes
	}
	return b
}

// validateProject checks the editable fields of p.
func validateProject(p *Project, lim Limits) error {
	var err error
	if p.Name, err = line("name", p.Name, MaxNameRunes, true); err != nil {
		return err
	}
	repo, err := runs.ValidateRepo(strings.TrimSpace(p.Repo), lim.RepoHosts)
	if err != nil {
		return runsError("repo", err)
	}
	p.Repo = repo
	if p.Branch = strings.TrimSpace(p.Branch); p.Branch == "" {
		p.Branch = "main"
	}
	if err := runs.ValidateBranch(p.Branch); err != nil {
		return runsError("branch", err)
	}
	if p.Description, err = line("description", p.Description, MaxDescriptionRunes, false); err != nil {
		return err
	}
	if p.Service = strings.TrimSpace(p.Service); p.Service != "" && !serviceRe.MatchString(p.Service) {
		return fieldErr("service", "servicio no válido")
	}
	if p.URL = strings.TrimSpace(p.URL); p.URL != "" && !validHTTPS(p.URL) {
		return fieldErr("url", "la URL debe ser https://")
	}
	if p.Notes, err = text("notes", p.Notes, MaxNotesBytes, false); err != nil {
		return err
	}
	return nil
}

// validHTTPS accepts an absolute https URL with a host and no userinfo.
func validHTTPS(raw string) bool {
	if len(raw) > 300 || strings.ContainsAny(raw, " \t\r\n<>\"'") {
		return false
	}
	u, err := url.Parse(raw)
	return err == nil && u.Scheme == "https" && u.Host != "" && u.User == nil && u.Opaque == ""
}

func validateMemoryText(s string) (string, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return "", fieldErr("text", "la lección está vacía")
	}
	if !utf8.ValidString(s) || strings.ContainsRune(s, 0) {
		return "", fieldErr("text", "no es texto UTF-8 válido")
	}
	if utf8.RuneCountInString(s) > MaxMemoryRunes {
		return "", fieldErr("text", fmt.Sprintf("supera %d caracteres", MaxMemoryRunes))
	}
	return s, nil
}

// validateSchedule normalizes days and checks the rest of s's own
// fields (the target is checked against the office state by the caller).
func validateSchedule(s *Schedule) error {
	var err error
	if s.Name, err = line("name", s.Name, MaxNameRunes, true); err != nil {
		return err
	}
	if len(s.Days) == 0 {
		return fieldErr("days", "elige al menos un día")
	}
	seen := map[int]bool{}
	days := []int{}
	for _, d := range s.Days {
		if d < 0 || d > 6 {
			return fieldErr("days", "los días van de 0 (domingo) a 6 (sábado)")
		}
		if !seen[d] {
			seen[d] = true
			days = append(days, d)
		}
	}
	slices.Sort(days)
	s.Days = days
	if !timeRe.MatchString(s.Time) {
		return fieldErr("time", "la hora debe ser HH:MM (UTC)")
	}
	return nil
}

// userKinds are the kinds a person can ask for; retro and juez are
// created by the office itself.
var userKinds = []string{KindAsk, KindPlan, KindChange, KindReview}

func validKind(k string) bool { return slices.Contains(userKinds, k) }
