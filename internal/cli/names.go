package cli

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/registry"
)

// goChoiceHelp is the help of the go target, shared by check-name and
// monorepo check-names.
const goChoiceHelp = "the Go package name the candidate implies, judged offline (no network): invalid when it is not a Go identifier, " +
	"is a keyword, or is the blank identifier (the Go spec refuses these as a package clause); taken when it is the name of a Go " +
	"standard-library package (the last element of its import path, from a committed `go list std` table), since every file importing " +
	"both needs an alias; discouraged when it has uppercase letters or underscores (Effective Go) or is one of Go's built-in identifiers " +
	"such as `len` or `string` (reason `predeclared`); available otherwise"

// checkNameHelp is check-name's help.
const checkNameHelp = "Check whether one or more package names are usable. npm and PyPI are asked over the network, through the package-level " +
	"pages that list a package's versions, whether the name and every name that collides with it after normalization are registered; " +
	"go is an offline check of the Go package name a candidate implies. Each name gets a status of available, taken, invalid (go only), " +
	"discouraged (go only), or error; a similar name the registry cannot answer about makes the status error, never available. " +
	"Accepts several names and repeated --target, and waits --delay milliseconds between networked requests. " +
	"Exits 0 when every name is available, 2 when any check ended in an error, and 1 otherwise: taken, invalid, and discouraged all " +
	"exit 1, so a discouraged Go name exits 1 even though Go accepts it."

// claimNameHelp is claim-name's help.
const claimNameHelp = "Claim a name on a package registry by publishing a minimal placeholder package (version 0.0.0). Runs the check-name " +
	"check first and publishes only a name it reports as available: a name reported taken is refused with exit 1, and a check that ended " +
	"in an error or returned any other status is refused with exit 2. npm authenticates with NPM_TOKEN when it is set, otherwise with " +
	"npm's own ~/.npmrc login; PyPI with UV_PUBLISH_TOKEN or PYPI_TOKEN when set, otherwise with the token in ~/.pypirc. With neither, " +
	"the claim is refused naming both places. No token is ever printed or passed as an argument. Registry names are held forever: " +
	"neither npm nor PyPI gives a claimed name back."

// The grant claim-name publishes under.
const publishGrant = "publish"

func registerNames(r *commandSet) {
	r.add(command{
		path:   []string{"check-name"},
		help:   checkNameHelp,
		effect: readOnly,
		args: []strictcli.Arg{
			strictcli.NewArg("names", "Package names to check", strictcli.ArgRequired(), strictcli.Variadic()),
		},
		flags: []strictcli.Flag{
			strictcli.StringFlag("target", "Registry or rule set to check each name against; repeatable",
				strictcli.Required(), strictcli.Repeatable(), strictcli.Unique(true),
				strictcli.Choices(
					strictcli.Ch("npm", "the npm registry"),
					strictcli.Ch("pypi", "the Python Package Index"),
					strictcli.Ch("go", goChoiceHelp),
				)),
			strictcli.IntFlag("delay", "Milliseconds to wait between consecutive registry requests (the offline go check never waits)",
				strictcli.Default(200)),
		},
		payload: checkNamePayloadSchema(),
		render:  renderCheckName,
		run:     runCheckName,
	})
	r.add(command{
		path:          []string{"claim-name"},
		help:          claimNameHelp,
		effect:        mutating,
		consequential: true,
		grants: []strictcli.Grant{newGrant(publishGrant,
			"claiming a name publishes a package to a public registry, and neither npm nor PyPI lets you take it back",
			strictcli.ProcMutate)},
		args: []strictcli.Arg{
			strictcli.NewArg("name", "The package name to claim", strictcli.ArgRequired()),
		},
		flags: []strictcli.Flag{
			strictcli.StringFlag("target", "The registry to publish the placeholder to", strictcli.Required(),
				strictcli.Choices(
					strictcli.Ch("npm", "publish the placeholder to the npm registry"),
					strictcli.Ch("pypi", "publish the placeholder to the Python Package Index"),
				)),
		},
		run: runClaimName,
	})
}

// strings of a repeatable or variadic value.
func stringList(v any) ([]string, error) {
	items, ok := v.([]interface{})
	if !ok {
		return nil, fmt.Errorf("expected a list, got %T", v)
	}
	out := make([]string, len(items))
	for i, item := range items {
		s, ok := item.(string)
		if !ok {
			return nil, fmt.Errorf("expected text, got %T", item)
		}
		out[i] = s
	}
	return out, nil
}

// nameResult is one verdict in check-name's payload.
type nameResult struct {
	Name                string              `json:"name"`
	Target              string              `json:"target"`
	Status              string              `json:"status"`
	Reason              *string             `json:"reason"`
	StructuredConflicts []registry.Conflict `json:"structured_conflicts"`
	RuleSentences       map[string]string   `json:"rule_sentences"`
	ExitCode            int                 `json:"exit_code"`
	Note                string              `json:"note,omitempty"`
	Error               string              `json:"error,omitempty"`
	Similar             []string            `json:"similar"`
	UltranormConflicts  []string            `json:"ultranorm_conflicts"`
	Checked             []string            `json:"checked"`
}

// checkNamePayload is check-name's payload: one verdict per name and target,
// target by target in the order given, names in the order given.
type checkNamePayload struct {
	DelayMS int          `json:"delay_ms"`
	Results []nameResult `json:"results"`
}

func payloadResult(r registry.NameResult) nameResult {
	out := nameResult{
		Name:                r.Name,
		Target:              string(r.Target),
		Status:              r.Status,
		StructuredConflicts: append([]registry.Conflict{}, r.Conflicts...),
		RuleSentences:       map[string]string{},
		ExitCode:            r.ExitCode(),
		Note:                r.Note,
		Error:               r.Error,
		Similar:             append([]string{}, r.Similar...),
		UltranormConflicts:  append([]string{}, r.UltranormConflicts...),
		Checked:             append([]string{}, r.Checked...),
	}
	if r.Reason != "" {
		reason := r.Reason
		out.Reason = &reason
	}
	for _, c := range r.Conflicts {
		out.RuleSentences[c.Rule] = registry.RuleSentences[c.Rule]
	}
	return out
}

func runCheckName(ctx *strictcli.Context, kw map[string]any) (any, error) {
	names, err := stringList(kw["names"])
	if err != nil {
		return nil, err
	}
	for _, n := range names {
		if n == "" {
			return nil, errors.New("a package name must not be empty")
		}
	}
	targets, err := stringList(kw["target"])
	if err != nil {
		return nil, err
	}
	delay := strictcli.Get[int](kw, "delay")
	if delay < 0 {
		return nil, fmt.Errorf("--delay must not be negative, got %d", delay)
	}
	client, err := registry.New(registry.Reads(ctx.Effects()))
	if err != nil {
		return nil, err
	}
	payload := checkNamePayload{DelayMS: delay, Results: []nameResult{}}
	worst := 0
	for _, target := range targets {
		results, err := client.CheckNames(registry.Ecosystem(target), names, time.Duration(delay)*time.Millisecond)
		if err != nil {
			return nil, err
		}
		for _, r := range results {
			payload.Results = append(payload.Results, payloadResult(r))
			worst = max(worst, r.ExitCode())
		}
	}
	if worst != 0 {
		return payload, &exitStatus{code: worst}
	}
	return payload, nil
}

func checkNameResultSchema() map[string]any {
	str := map[string]any{"type": "string"}
	strList := map[string]any{"type": "array", "items": str}
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"name":   str,
			"target": map[string]any{"type": "string", "enum": []any{"npm", "pypi", "go"}},
			"status": map[string]any{"type": "string", "enum": []any{"available", "taken", "invalid", "discouraged", "error"}},
			"reason": map[string]any{"type": []any{"string", "null"}, "enum": []any{nil, "registered", "stdlib", "moniker", "normalized", "ultranorm",
				"not-identifier", "keyword", "blank", "uppercase", "underscore", "predeclared"}},
			"structured_conflicts": map[string]any{"type": "array", "items": map[string]any{
				"type":                 "object",
				"properties":           map[string]any{"name": str, "rule": str},
				"required":             []any{"name", "rule"},
				"additionalProperties": false,
			}},
			"rule_sentences":      map[string]any{"type": "object", "additionalProperties": str},
			"exit_code":           map[string]any{"type": "integer"},
			"note":                str,
			"error":               str,
			"similar":             strList,
			"ultranorm_conflicts": strList,
			"checked":             strList,
		},
		"required":             []any{"name", "target", "status", "reason", "structured_conflicts", "rule_sentences", "exit_code", "similar", "ultranorm_conflicts", "checked"},
		"additionalProperties": false,
	}
}

func checkNamePayloadSchema() map[string]any {
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"delay_ms": map[string]any{"type": "integer"},
			"results":  map[string]any{"type": "array", "items": checkNameResultSchema()},
		},
		"required":             []any{"delay_ms", "results"},
		"additionalProperties": false,
	}
}

// decodePayload reads a payload in its plain JSON form back into T.
func decodePayload[T any](payload any) (T, error) {
	var out T
	data, err := json.Marshal(payload)
	if err != nil {
		return out, err
	}
	err = json.Unmarshal(data, &out)
	return out, err
}

// reasonExplanations explains a collision reason under the verdict.
var reasonExplanations = map[string]string{
	"stdlib":     "PyPI blocks names that match Python standard library modules.",
	"moniker":    "npm considers names identical after removing dashes, dots, and underscores.",
	"normalized": "The registry rejects names that normalize identically after stripping separators.",
	"ultranorm":  "PyPI blocks names that are visually similar (l/1/i and o/0 substitutions).",
}

// goHeadlines is the go verdict's headline by status.
var goHeadlines = map[string]string{
	"available":   "is available as a Go package name.",
	"taken":       "is taken as a Go package name.",
	"invalid":     "is not a valid Go package name.",
	"discouraged": "is a legal but discouraged Go package name.",
	"error":       "could not be judged as a Go package name.",
}

// registryDisplay is how a target's registry is spelled for a reader.
func registryDisplay(target string) string {
	if target == "pypi" {
		return "PyPI"
	}
	return target
}

// renderVerdict is the detailed rendering of one verdict.
func renderVerdict(r nameResult) string {
	var b strings.Builder
	line := func(format string, a ...any) { fmt.Fprintf(&b, format+"\n", a...) }
	reason := ""
	if r.Reason != nil {
		reason = *r.Reason
	}
	if r.Target == "go" {
		line("Checking Go package name %q (offline)...", r.Name)
		line("%q %s", r.Name, goHeadlines[r.Status])
		if r.Note != "" {
			line("  Note: %s", r.Note)
		}
		line("")
		line("Checked: %s", strings.Join(r.Checked, ", "))
		return strings.TrimRight(b.String(), "\n")
	}
	display := registryDisplay(r.Target)
	line("Checking %s for %q...", display, r.Name)
	if r.Status == "error" {
		line("Error checking %s: %s", display, r.Error)
		return strings.TrimRight(b.String(), "\n")
	}
	if r.Status == "available" {
		line("%q is available on %s.", r.Name, display)
	} else {
		line("%q is taken on %s.", r.Name, display)
	}
	if text, ok := reasonExplanations[reason]; ok {
		line("  %s", text)
	}
	if r.Note != "" {
		line("  Note: %s", r.Note)
	}
	if len(r.Similar) > 0 {
		line("")
		line("Similar names already taken:")
		for _, s := range r.Similar {
			line("  %s", s)
		}
		if r.Status == "available" {
			line("")
			line("Your name is available but has similar existing packages.")
		}
	}
	if len(r.UltranormConflicts) > 0 {
		line("")
		line("'%s' ultranormalizes to the same value as: %s", r.Name, strings.Join(r.UltranormConflicts, ", "))
	}
	if r.Target == "pypi" && r.Status == "available" {
		line("")
		line("Note: PyPI may also reject names on its prohibited names list (not publicly available).")
	}
	line("")
	line("Checked: %s", strings.Join(r.Checked, ", "))
	return strings.TrimRight(b.String(), "\n")
}

// summaryLine counts a table's verdicts: available, taken (ultranorm
// conflicts included), invalid and discouraged when present, then errors.
func summaryLine(statuses []string) string {
	counts := map[string]int{}
	for _, s := range statuses {
		counts[s]++
	}
	parts := []string{fmt.Sprintf("%d available", counts["available"]), fmt.Sprintf("%d taken", counts["taken"]+counts["CONFLICT"])}
	for _, label := range []string{"invalid", "discouraged"} {
		if counts[label] > 0 {
			parts = append(parts, fmt.Sprintf("%d %s", counts[label], label))
		}
	}
	if counts["error"] > 0 {
		parts = append(parts, fmt.Sprintf("%d error(s)", counts["error"]))
	}
	return fmt.Sprintf("Summary: %s (%d total)", strings.Join(parts, ", "), len(statuses))
}

// renderCheckName renders each target's verdicts: one name in detail, more
// as a table with a summary.
func renderCheckName(payload any) string {
	p, err := decodePayload[checkNamePayload](payload)
	if err != nil {
		return renderJSON(payload)
	}
	var order []string
	byTarget := map[string][]nameResult{}
	for _, r := range p.Results {
		if _, ok := byTarget[r.Target]; !ok {
			order = append(order, r.Target)
		}
		byTarget[r.Target] = append(byTarget[r.Target], r)
	}
	var blocks []string
	for _, target := range order {
		results := byTarget[target]
		if len(results) == 1 {
			blocks = append(blocks, renderVerdict(results[0]))
			continue
		}
		var rows [][]string
		var statuses []string
		for _, r := range results {
			status := r.Status
			if status != "error" && len(r.UltranormConflicts) > 0 {
				status = "CONFLICT"
			}
			rows = append(rows, []string{r.Name, status})
			statuses = append(statuses, status)
		}
		block := registryDisplay(target) + "\n" + renderTable([]string{"Name", "Status"}, rows) + "\n\n" + summaryLine(statuses)
		if target != "go" {
			block += fmt.Sprintf("\nChecked with %dms delay between names.", p.DelayMS)
			if p.DelayMS == 200 {
				block += " Increase --delay if rate limited."
			}
		}
		blocks = append(blocks, block)
	}
	return strings.Join(blocks, "\n\n")
}

func runClaimName(ctx *strictcli.Context, kw map[string]any) (any, error) {
	name := strictcli.Get[string](kw, "name")
	target := registry.Ecosystem(strictcli.Get[string](kw, "target"))
	if name == "" {
		return nil, errors.New("a package name must not be empty")
	}
	client, err := registry.New(registry.Reads(ctx.Effects()))
	if err != nil {
		return nil, err
	}
	verdict, err := client.CheckName(target, name, 0)
	if err != nil {
		return nil, err
	}
	switch verdict.Status {
	case registry.StatusAvailable:
	case registry.StatusTaken:
		detail := verdict.Note
		if detail == "" {
			detail = verdict.Reason
		}
		if len(verdict.UltranormConflicts) > 0 {
			detail = "visually similar to " + strings.Join(verdict.UltranormConflicts, ", ")
		}
		return nil, &exitStatus{code: 1, message: fmt.Sprintf("the name '%s' appears taken on %s: %s", name, target, detail)}
	case registry.StatusError:
		return nil, &exitStatus{code: 2, message: fmt.Sprintf("checking '%s' on %s: %s", name, target, verdict.Error)}
	default:
		return nil, &exitStatus{code: 2, message: fmt.Sprintf("the availability check for '%s' on %s returned the status '%s'; claim-name publishes only a name the check reports as available", name, target, verdict.Status)}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("the home directory, where ~/.npmrc and ~/.pypirc live, cannot be found: %w", err)
	}
	creds, err := registry.ClaimCredentials(target, os.LookupEnv, home)
	if err != nil {
		return nil, err
	}
	if err := registry.ClaimPlaceholder(ctx.Effects(), target, name, creds, publishGrant); err != nil {
		return nil, err
	}
	if !ctx.DryRun() {
		ctx.Out(fmt.Sprintf("Claimed '%s' on %s: %s", name, registryDisplay(string(target)), registry.ClaimURL(target, name)))
	}
	return nil, nil
}
