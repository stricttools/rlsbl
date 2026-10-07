package checks

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/targets"
)

// The support matrix family: the committed support matrix, which only
// rlsbl's own repository carries, against a fresh rendering from the
// targets' facts.
func matrixChecks() []check {
	return []check{
		errorCheck("target-matrix-fresh", checkTargetMatrixFresh),
	}
}

// matrixDriftLimit bounds the differences a stale matrix finding names.
const matrixDriftLimit = 10

func checkTargetMatrixFresh(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	path := filepath.Join(c.Root(), filepath.FromSlash(targets.MatrixPath))
	committed, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return r.Skipped(fmt.Sprintf("no %s in this repository: only rlsbl's own carries the support matrix", targets.MatrixPath))
	}
	if err != nil {
		panic(unanswered(err.Error()))
	}
	fresh, err := targets.RenderMatrix()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if string(committed) == string(fresh) {
		return r.Passed(targets.MatrixPath + " matches the targets' facts")
	}
	problems := []string{fmt.Sprintf("%s no longer matches the targets' facts it is generated from; regenerate it with `%s` and commit the result", targets.MatrixPath, targets.RegenerateCommand)}
	problems = append(problems, matrixDrift(committed, fresh)...)
	return reportErrors(r, problems, targets.MatrixPath+" is stale", "")
}

// matrixDrift names what differs between the committed matrix and a fresh
// rendering: a byte comparison decides staleness, and this only makes the
// finding say where. Committed text that is not JSON is itself the finding.
func matrixDrift(committed, fresh []byte) []string {
	var old, now map[string]any
	if err := json.Unmarshal(committed, &old); err != nil {
		return []string{fmt.Sprintf("the committed file is not JSON: %v", err)}
	}
	if err := json.Unmarshal(fresh, &now); err != nil {
		return []string{fmt.Sprintf("the fresh rendering is not JSON: %v", err)}
	}
	var details []string
	for _, section := range unionKeys(old, now) {
		o, inOld := old[section]
		n, inNew := now[section]
		switch {
		case !inOld:
			details = append(details, fmt.Sprintf("the section %q is missing from the committed file", section))
		case !inNew:
			details = append(details, fmt.Sprintf("the section %q no longer exists but is still committed", section))
		case !reflect.DeepEqual(o, n):
			details = append(details, fmt.Sprintf("the section %q differs", section))
		}
	}
	oldTargets, newTargets := targetRows(old["targets"]), targetRows(now["targets"])
	for _, name := range unionKeys(oldTargets, newTargets) {
		o, inOld := oldTargets[name].(map[string]any)
		n, inNew := newTargets[name].(map[string]any)
		switch {
		case !inOld:
			details = append(details, fmt.Sprintf("the target %q has no row in the committed file", name))
			continue
		case !inNew:
			details = append(details, fmt.Sprintf("the target %q is committed but no longer supported", name))
			continue
		}
		for _, axis := range unionKeys(o, n) {
			if !reflect.DeepEqual(o[axis], n[axis]) {
				details = append(details, fmt.Sprintf("the target %q, axis %q: committed %v, derived %v", name, axis, o[axis], n[axis]))
			}
		}
	}
	if len(details) > matrixDriftLimit {
		hidden := len(details) - matrixDriftLimit
		details = append(details[:matrixDriftLimit], fmt.Sprintf("and %d further difference(s)", hidden))
	}
	return details
}

// targetRows are the matrix's target rows by name.
func targetRows(section any) map[string]any {
	rows := map[string]any{}
	list, _ := section.([]any)
	for _, row := range list {
		if m, ok := row.(map[string]any); ok {
			if name, ok := m["name"].(string); ok {
				rows[name] = m
			}
		}
	}
	return rows
}

// unionKeys are the keys of a and b, sorted.
func unionKeys(a, b map[string]any) []string {
	seen := map[string]bool{}
	var keys []string
	for _, m := range []map[string]any{a, b} {
		for k := range m {
			if !seen[k] {
				seen[k] = true
				keys = append(keys, k)
			}
		}
	}
	sort.Strings(keys)
	return keys
}
