package office

import (
	"slices"

	"apptolast.com/ax-web/internal/harness"
)

// Effort sets of each CLI, as harness.Validate accepts them.
var (
	claudeEfforts = harness.ClaudeEfforts
	codexEfforts  = harness.CodexEfforts
)

// catalog is what the pinned CLIs know, verified on 2026-10-02: Claude
// Code 2.1.274 (`claude --help`) and Codex 0.156.1 (`codex debug models`,
// the entries with visibility "list"), both from the pinned ax-agents
// image. It is copied here, never read at run time.
var catalog = Catalog{
	Claude: HarnessCatalog{
		Version: "2.1.274",
		Source:  "claude --help de la imagen ax-agents fijada",
		Efforts: claudeEfforts,
		Models: []CatalogModel{
			{ID: "fable", Label: "Fable (alias, el más reciente)"},
			{ID: "opus", Label: "Opus (alias)"},
			{ID: "sonnet", Label: "Sonnet (alias)"},
			{ID: "haiku", Label: "Haiku (alias)"},
			{ID: "claude-fable-5-1", Label: "Fable 5.1"},
			{ID: "claude-opus-5-5", Label: "Opus 5.5"},
			{ID: "claude-sonnet-5-5", Label: "Sonnet 5.5"},
			{ID: "claude-haiku-4-5-20251001", Label: "Haiku 4.5"},
		},
	},
	Codex: HarnessCatalog{
		Version: "0.156.1",
		Source:  "codex debug models de la imagen ax-agents fijada",
		Efforts: codexEfforts,
		Models: []CatalogModel{
			{ID: "gpt-6-astra", Label: "GPT-6-Astra · máxima capacidad para el trabajo más exigente",
				Efforts: []string{"low", "medium", "high", "xhigh", "max", "ultra"}, Default: "low"},
			{ID: "gpt-6-sol", Label: "GPT-6-Sol · modelo de diario para programar",
				Efforts: []string{"low", "medium", "high", "xhigh", "max", "ultra"}, Default: "medium"},
			{ID: "gpt-6-luna", Label: "GPT-6-Luna · rápido y económico para tareas sencillas",
				Efforts: []string{"low", "medium", "high", "xhigh", "max"}, Default: "medium"},
			{ID: "gpt-5.6-sol", Label: "GPT-5.6-Sol · anterior, para trabajo de código complejo",
				Efforts: []string{"low", "medium", "high", "xhigh", "max", "ultra"}, Default: "low"},
			{ID: "gpt-5.6-terra", Label: "GPT-5.6-Terra · anterior, equilibrado",
				Efforts: []string{"low", "medium", "high", "xhigh", "max", "ultra"}, Default: "medium"},
			{ID: "gpt-5.6-luna", Label: "GPT-5.6-Luna · anterior, rápido y eficiente",
				Efforts: []string{"low", "medium", "high", "xhigh", "max"}, Default: "medium"},
			{ID: "gpt-5.5", Label: "GPT-5.5 · heredado, para código",
				Efforts: []string{"low", "medium", "high", "xhigh"}, Default: "medium"},
		},
	},
}

// CatalogInfo returns a copy of the catalogue.
func CatalogInfo() Catalog {
	c := catalog
	c.Claude.Models = slices.Clone(c.Claude.Models)
	c.Codex.Models = slices.Clone(c.Codex.Models)
	return c
}

func harnessCatalog(h string) *HarnessCatalog {
	switch h {
	case harness.Claude:
		return &catalog.Claude
	case harness.Codex:
		return &catalog.Codex
	}
	return nil
}

// effortsFor is the effort set a model accepts: its own list when the
// model is catalogued with one, else the harness's.
func effortsFor(h, model string) []string {
	hc := harnessCatalog(h)
	if hc == nil {
		return nil
	}
	for _, m := range hc.Models {
		if m.ID == model && len(m.Efforts) > 0 {
			return m.Efforts
		}
	}
	return hc.Efforts
}
