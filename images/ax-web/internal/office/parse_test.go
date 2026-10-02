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
