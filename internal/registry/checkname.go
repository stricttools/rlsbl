package registry

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

// The rule tokens naming why a conflicting name collides with a candidate.
// They are a contract: --json consumers key off them.
const (
	RuleNpmMoniker    = "npm-moniker"
	RulePypiSeparator = "pypi-separator"
	RulePypiUltranorm = "pypi-ultranorm"
	RuleStdlib        = "stdlib"
	RuleGoStdlib      = "go-stdlib"
)

// RuleSentences is the human sentence for each rule token.
var RuleSentences = map[string]string{
	RuleNpmMoniker:    "npm strips dashes, dots, and underscores: these share one moniker",
	RulePypiSeparator: "PyPI normalizes dashes, underscores, and dots to hyphens: these resolve identically",
	RulePypiUltranorm: "PyPI blocks names that are visually similar (l/1/i and o/0 substitutions)",
	RuleStdlib:        "PyPI blocks names that match Python standard library modules",
	RuleGoStdlib:      "a Go package named like a standard library package forces an import alias on every file that imports both",
}

// Conflict is one name a candidate collides with, and the rule that makes
// them collide.
type Conflict struct {
	Name string `json:"name"`
	Rule string `json:"rule"`
}

// NameResult is the verdict on one name for one target.
type NameResult struct {
	Name   string
	Target Ecosystem
	// Status is available, taken, error, and for go invalid or discouraged.
	Status string
	// Reason is why the name is not available: registered, stdlib, moniker,
	// normalized, ultranorm, or one of the go reasons (not-identifier,
	// keyword, blank, uppercase, underscore, predeclared); empty when
	// available or on error.
	Reason string
	// Conflicts are the colliding names, sorted by name and rule.
	Conflicts []Conflict
	// Note is the sentence explaining a collision or a go verdict.
	Note string
	// Error is why the check could not be made (status error).
	Error string
	// Similar are taken names that look like the candidate but do not
	// collide with it (npm and PyPI).
	Similar []string
	// UltranormConflicts are the PyPI names that collide with the candidate
	// once l, 1, i and o, 0 are folded.
	UltranormConflicts []string
	// Checked names the checks that ran, in order.
	Checked []string
	// Offline is true when no registry was asked (go).
	Offline bool
}

// ExitCode is the exit status the verdict gives check-name: 2 for an error,
// 0 for available, and 1 for anything else (taken, and for go invalid or
// discouraged, so a discouraged Go name exits 1 though Go accepts it).
func (r NameResult) ExitCode() int {
	switch r.Status {
	case StatusError:
		return 2
	case StatusAvailable:
		return 0
	}
	return 1
}

// addConflicts adds names under rule and keeps the list sorted by name and
// rule, so a name colliding through two rules contributes two entries.
func (r *NameResult) addConflicts(names []string, rule string) {
	for _, n := range names {
		r.Conflicts = append(r.Conflicts, Conflict{Name: n, Rule: rule})
	}
	sort.Slice(r.Conflicts, func(i, j int) bool {
		if r.Conflicts[i].Name != r.Conflicts[j].Name {
			return r.Conflicts[i].Name < r.Conflicts[j].Name
		}
		return r.Conflicts[i].Rule < r.Conflicts[j].Rule
	})
}

func (r *NameResult) fail(err error) {
	r.Status = StatusError
	r.Reason = ""
	r.Error = err.Error()
}

// quoteList renders names as 'a', 'b'.
func quoteList(names []string) string {
	q := make([]string, len(names))
	for i, n := range names {
		q[i] = "'" + n + "'"
	}
	return strings.Join(q, ", ")
}

// CheckNames checks every name against one target, waiting delay between
// names that asked a registry.
func (c Client) CheckNames(eco Ecosystem, names []string, delay time.Duration) ([]NameResult, error) {
	var out []NameResult
	for i, name := range names {
		r, err := c.CheckName(eco, name, delay)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
		if i < len(names)-1 && !r.Offline {
			c.sleep(delay)
		}
	}
	return out, nil
}

// CheckName judges one name on one target. npm and PyPI are asked whether
// the name, and every name that collides with it after normalization, is
// registered; go is judged offline (JudgeGoPackageName). delay is the wait
// between consecutive registry requests. A registry that cannot answer
// makes the verdict an error; only an unknown target is an error return.
func (c Client) CheckName(eco Ecosystem, name string, delay time.Duration) (NameResult, error) {
	switch eco {
	case Npm:
		return c.checkNpm(name, delay), nil
	case Pypi:
		return c.checkPypi(name, delay), nil
	case Go:
		v := JudgeGoPackageName(name)
		r := NameResult{Name: name, Target: Go, Status: v.Status, Reason: v.Reason, Note: v.Note, Offline: true,
			Checked: []string{"Go package-name rules", "Go standard library (offline)"}}
		r.addConflicts(v.Conflicts, RuleGoStdlib)
		return r, nil
	}
	return NameResult{}, fmt.Errorf("unknown target %q (check-name knows npm, pypi, and go)", eco)
}

// npmTaken asks the npm registry whether a package called name exists.
func (c Client) npmTaken(name string) (bool, error) {
	u := npmDocumentURL(name)
	r, err := c.get(u, npmAbbreviated)
	if err != nil {
		return false, err
	}
	switch r.status {
	case 200:
		return true, nil
	case 404:
		return false, nil
	}
	return false, unexpected(u, r)
}

// pypiTaken asks PyPI's simple index whether a project called name is
// registered; the simple index answers for a registered project with no
// release, which the JSON document does not.
func (c Client) pypiTaken(name string) (bool, error) {
	u := pypiSimpleURL(NormalizePypi(name))
	r, err := c.get(u)
	if err != nil {
		return false, err
	}
	switch r.status {
	case 200:
		return true, nil
	case 404:
		return false, nil
	}
	return false, unexpected(u, r)
}

// takenVariants asks about each variant in turn, waiting delay between
// requests, and returns the taken ones. A variant the registry cannot
// answer about is an error: an unasked variant might be the collision.
func (c Client) takenVariants(variants []string, taken func(string) (bool, error), delay time.Duration) ([]string, error) {
	var out []string
	for i, v := range variants {
		if i > 0 && delay > 0 {
			c.sleep(delay)
		}
		ok, err := taken(v)
		if err != nil {
			return nil, fmt.Errorf("checking the similar name '%s': %w", v, err)
		}
		if ok {
			out = append(out, v)
		}
	}
	return out, nil
}

// npmVariants are the names npm's moniker rule could fold onto name: every
// separator swapped for each separator, the separators removed, and, for a
// name without separators, each separator inserted at every interior
// position. Sorted; name itself excluded.
func npmVariants(name string) []string {
	lower := strings.ToLower(name)
	set := map[string]bool{}
	stripped := npmSeparators.ReplaceAllString(lower, "")
	set[stripped] = true
	for _, sep := range []string{"-", ".", "_"} {
		set[npmSeparators.ReplaceAllString(lower, sep)] = true
	}
	if stripped == lower {
		for i := 1; i < len(lower); i++ {
			for _, sep := range []string{"-", ".", "_"} {
				set[lower[:i]+sep+lower[i:]] = true
			}
		}
	}
	delete(set, name)
	return sortedKeys(set)
}

// pypiInsertionCap bounds the separator insertions tried for a PyPI name
// without separators.
const pypiInsertionCap = 30

// pypiVariants are the names PyPI's normalization could fold onto name.
// capped is true when the separator insertions stopped at pypiInsertionCap.
func pypiVariants(name string) (variants []string, capped bool) {
	lower := strings.ToLower(name)
	set := map[string]bool{
		NormalizePypi(name): true,
		pypiSeparators.ReplaceAllString(lower, "_"): true,
		pypiSeparators.ReplaceAllString(lower, "-"): true,
	}
	stripped := pypiSeparators.ReplaceAllString(lower, "")
	set[stripped] = true
	if stripped == lower {
		count := 0
		for i := 1; i < len(lower) && !capped; i++ {
			for _, sep := range []string{"-", "_", "."} {
				set[lower[:i]+sep+lower[i:]] = true
				count++
			}
			capped = count >= pypiInsertionCap && i < len(lower)-1
		}
	}
	delete(set, name)
	return sortedKeys(set), capped
}

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ultranormalize is a name with its separators removed, l, L, i, I folded to
// 1 and o, O to 0, and lowercased: the form PyPI compares for visual
// similarity.
func ultranormalize(name string) string {
	var b strings.Builder
	for _, r := range pypiSeparators.ReplaceAllString(name, "") {
		switch r {
		case 'l', 'L', 'i', 'I':
			b.WriteByte('1')
		case 'o', 'O':
			b.WriteByte('0')
		default:
			b.WriteString(strings.ToLower(string(r)))
		}
	}
	return b.String()
}

// ultranormVariantCap bounds the visually similar names tried for PyPI; a
// name with more combinations is reported as an error, never as checked.
const ultranormVariantCap = 64

// ultranormVariants are the names sharing name's ultranormalized form:
// every combination of l/1, o/0, and i/1 over the PEP 503 normalized name,
// excluding that name. capped is true when there are more than
// ultranormVariantCap combinations.
func ultranormVariants(name string) (variants []string, capped bool) {
	normalized := NormalizePypi(name)
	var options [][]string
	total := 1
	for _, r := range normalized {
		var opts []string
		switch r {
		case 'l', '1':
			opts = []string{"l", "1"}
		case 'o', '0':
			opts = []string{"o", "0"}
		case 'i':
			opts = []string{"i", "1"}
		default:
			opts = []string{string(r)}
		}
		options = append(options, opts)
		total *= len(opts)
		if total > ultranormVariantCap {
			return nil, true
		}
	}
	index := make([]int, len(options))
	for {
		var b strings.Builder
		for i, opts := range options {
			b.WriteString(opts[index[i]])
		}
		if v := b.String(); v != normalized {
			variants = append(variants, v)
		}
		// Advance like an odometer, last position fastest.
		i := len(index) - 1
		for i >= 0 {
			index[i]++
			if index[i] < len(options[i]) {
				break
			}
			index[i] = 0
			i--
		}
		if i < 0 {
			return variants, false
		}
	}
}

func (c Client) checkNpm(name string, delay time.Duration) NameResult {
	r := NameResult{Name: name, Target: Npm, Checked: []string{"npm"}}
	taken, err := c.npmTaken(name)
	if err != nil {
		r.fail(err)
		return r
	}
	if taken {
		r.Status, r.Reason = StatusTaken, "registered"
		return r
	}
	r.Status = StatusAvailable
	r.Checked = append(r.Checked, "variants")
	takenVariants, err := c.takenVariants(npmVariants(name), c.npmTaken, delay)
	if err != nil {
		r.fail(err)
		return r
	}
	var hard []string
	for _, v := range takenVariants {
		if NormalizeNpm(v) == NormalizeNpm(name) {
			hard = append(hard, v)
		} else {
			r.Similar = append(r.Similar, v)
		}
	}
	if len(hard) > 0 {
		r.Status, r.Reason = StatusTaken, "moniker"
		r.Note = fmt.Sprintf("moniker collision with %s — %s", quoteList(hard), RuleSentences[RuleNpmMoniker])
		r.addConflicts(hard, RuleNpmMoniker)
	}
	r.Checked = append(r.Checked, "moniker similarity")
	conflicts, err := c.npmSearchConflicts(name)
	if err != nil {
		// A search failure leaves the verdict unknown unless a collision is
		// already established.
		if r.Status != StatusTaken {
			r.fail(fmt.Errorf("npm moniker check failed: %w", err))
		}
		return r
	}
	if len(conflicts) > 0 && r.Status != StatusTaken {
		r.Status, r.Reason = StatusTaken, "moniker"
		r.Note = fmt.Sprintf("moniker conflict with %s — %s", quoteList(conflicts), RuleSentences[RuleNpmMoniker])
		r.addConflicts(conflicts, RuleNpmMoniker)
	}
	return r
}

// npmSearchConflicts searches the registry for packages like name and
// returns those sharing its moniker under another spelling.
func (c Client) npmSearchConflicts(name string) ([]string, error) {
	u := npmSearchURL(name)
	r, err := c.get(u)
	if err != nil {
		return nil, err
	}
	if r.status != 200 {
		return nil, unexpected(u, r)
	}
	var doc struct {
		Objects []struct {
			Package struct {
				Name string `json:"name"`
			} `json:"package"`
		} `json:"objects"`
	}
	if err := json.Unmarshal(r.body, &doc); err != nil {
		return nil, fmt.Errorf("GET %s answered something that is not a search result: %w", u, err)
	}
	var out []string
	for _, o := range doc.Objects {
		if n := o.Package.Name; n != "" && n != name && NormalizeNpm(n) == NormalizeNpm(name) {
			out = append(out, n)
		}
	}
	return out, nil
}

func (c Client) checkPypi(name string, delay time.Duration) NameResult {
	r := NameResult{Name: name, Target: Pypi, Checked: []string{"PyPI", "stdlib"}}
	if module, ok := pythonStdlibCollision(name); ok {
		r.Status, r.Reason = StatusTaken, "stdlib"
		r.Note = fmt.Sprintf("conflicts with Python stdlib module '%s'", module)
		r.addConflicts([]string{module}, RuleStdlib)
		return r
	}
	taken, err := c.pypiTaken(name)
	if err != nil {
		r.fail(err)
		return r
	}
	if taken {
		r.Status, r.Reason = StatusTaken, "registered"
		return r
	}
	r.Status = StatusAvailable
	variants, capped := pypiVariants(name)
	if capped {
		r.Checked = append(r.Checked, fmt.Sprintf("variants (separator insertions capped at %d)", pypiInsertionCap))
	} else {
		r.Checked = append(r.Checked, "variants")
	}
	takenVariants, err := c.takenVariants(variants, c.pypiTaken, delay)
	if err != nil {
		r.fail(err)
		return r
	}
	var hard []string
	for _, v := range takenVariants {
		if ultranormalize(v) == ultranormalize(name) {
			hard = append(hard, v)
		} else {
			r.Similar = append(r.Similar, v)
		}
	}
	if len(hard) > 0 {
		r.Status, r.Reason = StatusTaken, "normalized"
		r.Note = fmt.Sprintf("normalization collision with %s — %s", quoteList(hard), RuleSentences[RulePypiSeparator])
		r.addConflicts(hard, RulePypiSeparator)
		return r
	}
	r.Checked = append(r.Checked, "ultranormalization")
	similar, capped := ultranormVariants(name)
	if capped {
		r.fail(fmt.Errorf("too many ambiguous characters in '%s': visually similar names number more than %d, so the ultranormalization check cannot be completed", name, ultranormVariantCap))
		return r
	}
	for i, v := range similar {
		if i > 0 && delay > 0 {
			c.sleep(delay)
		}
		ok, err := c.pypiTaken(v)
		if err != nil {
			r.fail(fmt.Errorf("checking the visually similar name '%s': %w", v, err))
			return r
		}
		if ok {
			r.Status, r.Reason = StatusTaken, "ultranorm"
			r.UltranormConflicts = []string{v}
			r.addConflicts([]string{v}, RulePypiUltranorm)
			return r
		}
	}
	return r
}
