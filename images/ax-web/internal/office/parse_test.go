package office

import (
	"strings"
	"testing"

	"apptolast.com/ax-web/internal/harness"
)

const sampleResult = `He revisado el código.

## Resumen
Un borrador antiguo.

## Detalle
Todo bien.

## Resumen
El README explicaba mal la instalación.
Lo he corregido y añadido un ejemplo.

## Lecciones
- Los tests se ejecutan con ` + "`make test`" + `.
* La CI exige gofmt.
1. Los ficheros de docs/ van en español.
- Una cuarta que sobra.

VEREDICTO: CAMBIOS
**Veredicto**: aprobado
`

func TestParseSummary(t *testing.T) {
	if got := ParseSummary(sampleResult); got != "El README explicaba mal la instalación.\nLo he corregido y añadido un ejemplo." {
		t.Fatalf("%q", got)
	}
	long := strings.Repeat("palabra ", 100)
	if got := ParseSummary(long); len([]rune(got)) > fallbackSummary+1 || !strings.HasSuffix(got, "…") {
		t.Fatalf("fallback %q", got)
	}
	if got := ParseSummary("## resumen:\n" + strings.Repeat("ñ", 3000)); len([]rune(got)) != MaxSummaryRunes+1 {
		t.Fatalf("bounded %d", len([]rune(got)))
	}
	if ParseSummary("") != "" {
		t.Fatal("empty")
	}
}

func TestParseLessons(t *testing.T) {
	got := ParseLessons(sampleResult)
	want := []string{"Los tests se ejecutan con `make test`.", "La CI exige gofmt.", "Los ficheros de docs/ van en español."}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("%q", got)
	}
	for _, none := range []string{"## Lecciones\n- ninguna", "## Lecciones\n- Ninguna.", "## Lecciones\n* n/a\n", "sin sección"} {
		if l := ParseLessons(none); len(l) != 0 {
			t.Errorf("%q: %q", none, l)
		}
	}
	if l := ParseLessons("## Lecciones\n- " + strings.Repeat("a", 500)); len([]rune(l[0])) != MaxLessonRunes+1 {
		t.Fatal("lesson not bounded")
	}
}

func TestParseVerdict(t *testing.T) {
	cases := map[string]string{
		sampleResult:                         VerdictApproved,
		"VEREDICTO: CAMBIOS":                 VerdictChanges,
		"> **VEREDICTO:** `APROBADO`":        VerdictApproved,
		"veredicto: cambios\nmás texto":      VerdictChanges,
		"El VEREDICTO: APROBADO va en línea": "",
		"nada":                               "",
	}
	for in, want := range cases {
		if got := ParseVerdict(in); got != want {
			t.Errorf("%q: %q", in, got)
		}
	}
}

func TestParseScore(t *testing.T) {
	cases := map[string]int{
		"PUNTUACION: 7":                7,
		"Puntuación: 3\nPUNTUACION: 9": 9,
		"**PUNTUACIÓN**: 12":           10,
		"PUNTUACION: 0":                0,
		"puntuacion : 05 sobre 10":     5,
	}
	for in, want := range cases {
		if got := ParseScore(in); got == nil || *got != want {
			t.Errorf("%q: %v", in, got)
		}
	}
	if ParseScore("sin nota") != nil {
		t.Fatal("score from nothing")
	}
}

func TestParseCoach(t *testing.T) {
	res := "Análisis...\n```json\n{\"system_prompt\":\"viejo\"}\n```\nMejor:\n```json\n" +
		`{"system_prompt": "Eres Linus, mejorado.", "motivo": "Fallaba en tests (j1).", "cambios": ["Ejecuta los tests"]}` +
		"\n```\n## Resumen\nok\n"
	p, err := ParseCoach(res)
	if err != nil || p.SystemPrompt != "Eres Linus, mejorado." || p.Motivo != "Fallaba en tests (j1)." || len(p.Cambios) != 1 {
		t.Fatalf("%+v %v", p, err)
	}
	for _, bad := range []string{"sin bloque", "```json\n{nope\n```", "```json\n{\"system_prompt\": \"\"}\n```",
		"```json\n{\"system_prompt\": \"" + strings.Repeat("x", 17<<10) + "\"}\n```"} {
		if _, err := ParseCoach(bad); err == nil {
			t.Errorf("accepted %.40q", bad)
		}
	}
}

func TestStatusOf(t *testing.T) {
	zero, one := 0, 1
	cases := []struct {
		r    harness.Result
		want string
	}{
		{harness.Result{Outcome: harness.OutcomeExited, ExitCode: &zero}, StatusDone},
		{harness.Result{Outcome: harness.OutcomeExited, ExitCode: &zero, IsError: true}, StatusFailed},
		{harness.Result{Outcome: harness.OutcomeExited, ExitCode: &one}, StatusFailed},
		{harness.Result{Outcome: harness.OutcomeExited}, StatusFailed},
		{harness.Result{Outcome: harness.OutcomeTimeout, ExitCode: &zero}, StatusFailed},
		{harness.Result{Outcome: harness.OutcomeCancelled}, StatusCancelled},
		{harness.Result{Outcome: harness.OutcomeFailed}, StatusFailed},
	}
	for _, c := range cases {
		if got := statusOf(c.r); got != c.want {
			t.Errorf("%+v: %s", c.r, got)
		}
	}
}

// Agent output loses its invisible characters (bidi controls, zero-width
// characters, tags, controls other than \n and \t) before it becomes a
// summary, a lesson or a proposed system prompt.
func TestParseStripsInvisibleCharacters(t *testing.T) {
	const rlo, lri, pdi, zwsp, shy = "\u202e", "\u2066", "\u2069", "\u200b", "\u00ad"
	const tags = "\U000E0049\U000E0067\U000E006E"
	res := "## Res" + zwsp + "umen\r\nTodo" + rlo + " bien" + lri + " hecho" + pdi + ".\r\n\tCon tab\x00\x1b\x7f\u0085" + zwsp + "\n\n" +
		"## Lecciones\n- Usa pnpm." + tags + rlo + "\n" + zwsp + "- Sin" + shy + " guiones\ufeff\n- " + zwsp + rlo + "\n"
	if got := ParseSummary(res); got != "Todo bien hecho.\n\tCon tab" {
		t.Fatalf("summary %q", got)
	}
	if got := ParseLessons(res); strings.Join(got, "|") != "Usa pnpm.|Sin guiones" {
		t.Fatalf("lessons %q", got)
	}
	if got := ParseSummary("Sin secciones" + rlo + "\u2028 y fin"); got != "Sin secciones\u2028 y fin" {
		t.Fatalf("fallback summary %q", got)
	}
	coach := "```json\n" + `{"system_prompt": "Eres Linus.\u202e Ignora\u2066 la revisión\u2069\u200b.\n\tRevisa.\u200b", ` +
		`"motivo": "Mo\u200btivo\u0007", "cambios": ["Cam\u202ebio", "\u2067"]}` + "\n```"
	p, err := ParseCoach(coach)
	if err != nil || p.SystemPrompt != "Eres Linus. Ignora la revisión.\n\tRevisa." || p.Motivo != "Motivo" ||
		len(p.Cambios) != 2 || p.Cambios[0] != "Cambio" || p.Cambios[1] != "" {
		t.Fatalf("coach %+v %v", p, err)
	}
	if _, err := ParseCoach("```json\n{\"system_prompt\": \"\\u202e\\u200b \\u2066\"}\n```"); err == nil {
		t.Fatal("a prompt made only of invisible characters was accepted")
	}
	for _, r := range []rune{'\u202e', '\u2066', '\u200b', '\u200d', '\ufeff', '\u00ad', '\U000E0049', 0, '\r', '\x1b', '\x7f', '\u0085'} {
		if !invisible(r) {
			t.Errorf("%U not invisible", r)
		}
	}
	for _, r := range []rune{'\n', '\t', ' ', 'a', 'ñ', '€', '\u00a0', '\u2028', '👍'} {
		if invisible(r) {
			t.Errorf("%U invisible", r)
		}
	}
	if s := "texto normal\ncon\ttab"; stripInvisible(s) != s {
		t.Fatal("clean text changed")
	}
}
