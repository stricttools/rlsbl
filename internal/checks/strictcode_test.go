package checks

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// fakeStrictcodeScript answers `strictcode registry rules --json` and
// `strictcode analyze <dir> --json` from the files beside it, recording
// every argv, and refuses every other use.
const fakeStrictcodeScript = `#!/bin/sh
here="${0%/*}"
printf '%s\n' "$*" >> "$here/strictcode-calls.txt"
case "$1 $2" in
"registry rules") cat "$here/rules.json"; exit 0 ;;
"analyze "*) cat "$here/analyze.json"; exit "$(cat "$here/analyze.exit")" ;;
esac
echo "fake strictcode: unexpected arguments: $*" >&2
exit 97
`

// fakeStrictcode is a fake strictcode installed for one test.
type fakeStrictcode struct {
	t   *testing.T
	dir string
}

// installStrictcode puts a fake strictcode first on PATH, implementing the
// rules named.
func installStrictcode(t *testing.T, rules []string) *fakeStrictcode {
	t.Helper()
	f := &fakeStrictcode{t: t, dir: t.TempDir()}
	f.write("strictcode", fakeStrictcodeScript, 0o755)
	f.setRules(rules)
	f.setAnalysis(0, nil, nil)
	t.Setenv("PATH", f.dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return f
}

func (f *fakeStrictcode) write(name, content string, mode os.FileMode) {
	f.t.Helper()
	if err := os.WriteFile(filepath.Join(f.dir, name), []byte(content), mode); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fakeStrictcode) setRules(ids []string) {
	f.t.Helper()
	type rule struct {
		ID string `json:"id"`
	}
	rules := make([]rule, len(ids))
	for i, id := range ids {
		rules[i] = rule{ID: id}
	}
	f.writeJSON("rules.json", map[string]any{"exit_code": 0, "payload": map[string]any{"format_version": 1, "strictcode_version": "0.9.0", "rules": rules}})
}

// finding is one strictcode finding as analyze --json prints it.
type finding struct {
	Rule     string         `json:"rule"`
	Severity string         `json:"severity"`
	Message  string         `json:"message"`
	Target   map[string]any `json:"target"`
}

func (f *fakeStrictcode) setAnalysis(exit int, findings []finding, diagnostics []string) {
	f.t.Helper()
	diags := []map[string]string{}
	for _, d := range diagnostics {
		diags = append(diags, map[string]string{"level": "error", "message": d})
	}
	var payload any
	if exit != 2 {
		if findings == nil {
			findings = []finding{}
		}
		payload = map[string]any{"format_version": 1, "strictcode_version": "0.9.0", "workspace_root": "/repo", "findings": findings}
	}
	f.writeJSON("analyze.json", map[string]any{"exit_code": exit, "payload": payload, "diagnostics": diags})
	f.write("analyze.exit", strconv.Itoa(exit)+"\n", 0o644)
}

func (f *fakeStrictcode) writeJSON(name string, v any) {
	f.t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		f.t.Fatal(err)
	}
	f.write(name, string(data)+"\n", 0o644)
}

func (f *fakeStrictcode) calls() []string {
	f.t.Helper()
	data, err := os.ReadFile(filepath.Join(f.dir, "strictcode-calls.txt"))
	if err != nil {
		f.t.Fatal(err)
	}
	return strings.Split(strings.TrimSpace(string(data)), "\n")
}

func TestAMissingStrictcodeFailsNamingTheInstall(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	testsupport.PathOnly(t, "git", "sh", "cat")
	got := runCheck(t, inputs(t, r.Dir), "strictcode")
	mustStatus(t, got, "fail")
	mustMention(t, got, StrictcodeInstall)
	// What the install puts on PATH.
	installStrictcode(t, RequiredStrictcodeRules)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "strictcode"), "pass")
}

func TestAStrictcodeLackingARequiredRuleIsRefusedNamingEachAndComparingNoVersion(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	var partial []string
	for _, id := range RequiredStrictcodeRules {
		if id != "import-cycles" && id != "lint" {
			partial = append(partial, id)
		}
	}
	fake := installStrictcode(t, partial)
	got := runCheck(t, inputs(t, r.Dir), "strictcode")
	mustStatus(t, got, "fail")
	mustMention(t, got, "the rule import-cycles", "the rule lint,", StrictcodeInstall)
	for _, call := range fake.calls() {
		if strings.Contains(call, "version") {
			t.Errorf("the check asked strictcode for a version: %q", call)
		}
		if strings.HasPrefix(call, "analyze") {
			t.Errorf("the check analyzed with a strictcode lacking required rules: %q", call)
		}
	}
	// What installing a complete strictcode gives.
	fake.setRules(RequiredStrictcodeRules)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "strictcode"), "pass")
}

func TestStrictcodeFindingsAreReportedBySeverity(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	fake := installStrictcode(t, RequiredStrictcodeRules)
	fake.setAnalysis(1, []finding{
		{Rule: "deps-unused", Severity: "error", Message: "widget is declared and never imported", Target: map[string]any{"file": "pyproject.toml", "line": 7}},
		{Rule: "dead-modules", Severity: "warning", Message: "portal/old.py is never imported", Target: map[string]any{"file": "portal/old.py", "line": 1}},
	}, nil)
	got := runCheck(t, inputs(t, r.Dir), "strictcode")
	mustStatus(t, got, "fail")
	mustMention(t, got, "deps-unused: pyproject.toml:7: widget is declared and never imported", "dead-modules: portal/old.py:1")
	fake.setAnalysis(0, []finding{{Rule: "dead-modules", Severity: "warning", Message: "portal/old.py is never imported", Target: map[string]any{"file": "portal/old.py", "line": 1}}}, nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "strictcode"), "warn")
	calls := fake.calls()
	if last := calls[len(calls)-1]; !strings.HasPrefix(last, "analyze /") || !strings.HasSuffix(last, " --json") {
		t.Errorf("analyze ran as %q", last)
	}
}

func TestAStrictcodeThatCannotAnalyzeFails(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	fake := installStrictcode(t, RequiredStrictcodeRules)
	fake.setAnalysis(2, nil, []string{"strictcode.toml: unknown key surprise"})
	got := runCheck(t, inputs(t, r.Dir), "strictcode")
	mustStatus(t, got, "fail")
	mustMention(t, got, "exited 2", "unknown key surprise")
}
