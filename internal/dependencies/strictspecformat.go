package dependencies

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"

	"github.com/stricttools/strictspec/go/strictspec"
)

// StrictspecManifest is the file declaring which validators strictspec
// generates and where: the enumeration the generated-format evaluation
// reads, so no directory is guessed at.
const StrictspecManifest = "strictspec.toml"

// regenerateFix is the one fix every generated-format finding names.
const regenerateFix = "regenerate with `strictspec gen`"

// generatedFormatLine finds the GENERATED_CODE_FORMAT constant as each
// emitter writes it: `GENERATED_CODE_FORMAT = 1` (Python), `const
// GENERATED_CODE_FORMAT = 1` (Go), `export const GENERATED_CODE_FORMAT = 1;`
// (TypeScript).
var generatedFormatLine = regexp.MustCompile(`(?m)^(?:export\s+)?(?:const\s+)?GENERATED_CODE_FORMAT\s*=\s*(\d+)`)

// ReadGeneratedCodeFormat is the GENERATED_CODE_FORMAT a generated file
// declares, and false when it declares none.
func ReadGeneratedCodeFormat(path string) (int, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, false, fmt.Errorf("reading %s: %w", path, err)
	}
	m := generatedFormatLine.FindSubmatch(data)
	if m == nil {
		return 0, false, nil
	}
	n, err := strconv.Atoi(string(m[1]))
	if err != nil {
		return 0, false, fmt.Errorf("%s: GENERATED_CODE_FORMAT %q is not a number", path, m[1])
	}
	return n, true, nil
}

// generationTarget is one output strictspec.toml declares.
type generationTarget struct {
	output string
	lang   string
}

// declaredTargets are the outputs the strictspec.toml in dir declares;
// found is false when there is none.
func declaredTargets(dir string) ([]generationTarget, bool, error) {
	doc, found, err := readTOML(filepath.Join(dir, StrictspecManifest))
	if err != nil || !found {
		return nil, found, err
	}
	var out []generationTarget
	schemas, _ := doc["schemas"].([]any)
	for _, raw := range schemas {
		schema, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		targets, _ := schema["targets"].([]any)
		for _, rawTarget := range targets {
			t, ok := rawTarget.(map[string]any)
			if !ok {
				continue
			}
			output, _ := t["output"].(string)
			lang, _ := t["lang"].(string)
			if output != "" {
				out = append(out, generationTarget{output: output, lang: lang})
			}
		}
	}
	return out, true, nil
}

// GeneratedFormatVerdict is EvaluateGeneratedFormat's answer.
type GeneratedFormatVerdict struct {
	Verdict
	// SkipReason is set when dir generates no validators.
	SkipReason string
}

// EvaluateGeneratedFormat holds every validator dir's strictspec.toml
// declares against the generated-code formats the strictspec runtime reads,
// through the runtime's own predicate so the two never disagree. A format
// outside the range makes the validator refuse to load before it validates
// anything; a Python validator without the constant predates the format
// declaration, and every runtime refuses it. The fix is always to
// regenerate. No dependency floor is derived from the release a file names
// as its generator: pairing is on the format.
func EvaluateGeneratedFormat(dir string) (GeneratedFormatVerdict, error) {
	targets, found, err := declaredTargets(dir)
	if err != nil {
		return GeneratedFormatVerdict{}, err
	}
	if !found {
		return GeneratedFormatVerdict{SkipReason: fmt.Sprintf("no %s -- this project generates no validators", StrictspecManifest)}, nil
	}
	if len(targets) == 0 {
		return GeneratedFormatVerdict{SkipReason: StrictspecManifest + " declares no generation target"}, nil
	}
	low, high := strictspec.MinGeneratedCodeFormat, strictspec.MaxGeneratedCodeFormat
	var v Verdict
	read := 0
	for _, t := range targets {
		path := filepath.Join(dir, filepath.FromSlash(t.output))
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			v.Notes = append(v.Notes, t.output+": declared but not generated yet")
			continue
		} else if err != nil {
			return GeneratedFormatVerdict{}, err
		}
		format, declared, err := ReadGeneratedCodeFormat(path)
		if err != nil {
			return GeneratedFormatVerdict{}, err
		}
		if !declared {
			if t.lang == "python" {
				v.Problems = append(v.Problems, fmt.Sprintf("%s: no GENERATED_CODE_FORMAT, so it predates the format declaration and every strictspec runtime refuses it at import rather than reading it as format %d. The fix is to %s.", t.output, low, regenerateFix))
			} else {
				v.Notes = append(v.Notes, fmt.Sprintf("%s: no GENERATED_CODE_FORMAT to read (lang %s)", t.output, t.lang))
			}
			continue
		}
		if strictspec.CheckGeneratedCodeFormat(format, "unread") != nil {
			v.Problems = append(v.Problems, fmt.Sprintf("%s: declares generated-code format %d, which the strictspec runtime does not read (it reads %d..%d), so loading it fails before any document is validated. The fix is to %s.", t.output, format, low, high, regenerateFix))
			continue
		}
		read++
	}
	switch {
	case read > 0:
		v.Notes = append([]string{fmt.Sprintf("%d generated validator(s) declare a format the strictspec runtime reads (%d..%d)", read, low, high)}, v.Notes...)
	case len(v.Problems) == 0 && len(v.Notes) == 0:
		v.Notes = append(v.Notes, "no generated validator declares a format")
	}
	return GeneratedFormatVerdict{Verdict: v}, nil
}
