package workflows

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The CI router.
//
// GitHub refuses a workflow referencing 20 or more reusable workflows, so
// the router inlines every member's CI jobs instead of calling them. Each
// inlined job:
//
//   - is keyed <prefix>-<job>, where the prefix is <member>-ci for the
//     member's ci.yml and <member>-ci-<x> for its ci-<x>.yml;
//   - is named "<prefix> / <job name>", so its check runs carry the names
//     CheckPattern matches;
//   - needs the detect job and runs when the member's path filter matched
//     the push, or when the router was dispatched with run_all;
//   - keeps its own needs (rewritten to the prefixed keys), its own if (and
//     the router's condition), and the workflow-level env, defaults.run, and
//     permissions it would have inherited.

// RunAllInput is the router's workflow_dispatch input that runs every
// member's jobs whatever the path filters say. A release candidate whose
// commits honestly touch few members (a fix-forward) leaves the others'
// jobs skipped, and a skipped check proves nothing; dispatching the router
// at the candidate with run_all=true runs them on the same commit, and
// their verdicts supersede the skips. Nothing is waived: the jobs still
// have to pass. On a push, inputs.run_all is empty, so the filters decide.
const RunAllInput = "run_all"

// maxReusableCalls is the number of reusable-workflow calls (job-level
// uses) at which GitHub refuses a workflow. The routers inline jobs, so a
// router reaching it is refused well before GitHub would.
const maxReusableCalls = 20

// conflictMarkers are the lines a three-way merge leaves in a file it could
// not merge.
var conflictMarkers = regexp.MustCompile(`(?m)^(<{7}|={7}|>{7})( |$)`)

// placeholder is a scaffold template placeholder left in a file: "{{" not
// preceded by '$' (GitHub's own ${{ ... }} expressions pass).
var placeholder = regexp.MustCompile(`(^|[^$])\{\{[^}]*\}\}`)

// CISources are the absolute paths of a member's own CI workflows under
// <memberDir>/.github/workflows: ci.yml, then every ci-*.yml in name order,
// leaving out a generated workflow (Header), which is never a source. One
// discovery serves the router (which mints the job keys from it), the
// release's CI check, and the publish router (which name the check runs
// those keys produce).
func CISources(memberDir string) ([]string, error) {
	dir := filepath.Join(memberDir, filepath.FromSlash(Dir))
	entries, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", dir, err)
	}
	var names []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || name == RouterFile {
			continue
		}
		if name == "ci.yml" || (strings.HasPrefix(name, "ci-") && strings.HasSuffix(name, ".yml")) {
			names = append(names, name)
		}
	}
	sort.Slice(names, func(i, j int) bool {
		if (names[i] == "ci.yml") != (names[j] == "ci.yml") {
			return names[i] == "ci.yml"
		}
		return names[i] < names[j]
	})
	var sources []string
	for _, name := range names {
		path := filepath.Join(dir, name)
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", path, err)
		}
		if IsGenerated(string(data)) {
			continue
		}
		sources = append(sources, path)
	}
	return sources, nil
}

// JobPrefix is the job-key prefix of a member's CI file: <member>-ci for
// ci.yml, <member>-ci-<x> for ci-<x>.yml.
func JobPrefix(member, base string) string {
	if base == "ci.yml" {
		return member + "-ci"
	}
	return member + "-" + strings.TrimSuffix(base, ".yml")
}

// JobPrefixes are the job-key prefixes of a member's CI sources; none when
// it has no CI of its own.
func JobPrefixes(member, memberDir string) ([]string, error) {
	sources, err := CISources(memberDir)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, s := range sources {
		out = append(out, JobPrefix(member, filepath.Base(s)))
	}
	return out, nil
}

// CheckPattern is the pattern of the check-run names a member's inlined CI
// jobs produce, "^(<prefix>|...) / ", and false when the member has no CI of
// its own.
func CheckPattern(member, memberDir string) (string, bool, error) {
	prefixes, err := JobPrefixes(member, memberDir)
	if err != nil || len(prefixes) == 0 {
		return "", false, err
	}
	quoted := make([]string, len(prefixes))
	for i, p := range prefixes {
		quoted[i] = regexp.QuoteMeta(p)
	}
	return "^(" + strings.Join(quoted, "|") + ") / ", true, nil
}

// readWorkflowSource reads a member's workflow source, refusing merge
// conflict markers and scaffold placeholders left in it, and returns its
// root mapping; nil when it holds no jobs key.
func readWorkflowSource(path, rel string) (*yaml.Node, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", rel, err)
	}
	text := string(data)
	if loc := conflictMarkers.FindStringIndex(text); loc != nil {
		line := strings.Count(text[:loc[0]], "\n") + 1
		return nil, fmt.Errorf("%s holds merge conflict markers (line %d), left by a scaffold merge; resolve the marked regions and run this again", rel, line)
	}
	if m := placeholder.FindStringSubmatch(text); m != nil {
		return nil, fmt.Errorf("%s holds the template placeholder %s, which scaffold leaves only in a file it did not finish; run `rlsbl scaffold` in the member and run this again", rel, strings.TrimPrefix(m[0], m[1]))
	}
	root, err := parseDocument(text, rel)
	if err != nil {
		return nil, err
	}
	if root == nil || !isMap(root) {
		return nil, fmt.Errorf("%s is not a workflow: its document is not a mapping", rel)
	}
	jobs := mapGet(root, "jobs")
	if jobs == nil {
		return nil, nil
	}
	if !isMap(jobs) {
		return nil, fmt.Errorf("%s: jobs is not a mapping (line %d)", rel, jobs.Line)
	}
	return root, nil
}

// versionFileInputs are the setup action inputs naming a file relative to
// the repository root rather than the working directory.
var versionFileInputs = []string{"go-version-file", "python-version-file", "node-version-file"}

// rootPath composes a path relative to the member under the member's path,
// once: a path already under it, or absolute, is left alone.
func rootPath(memberPath, rel string) string {
	if memberPath == declarations.RootPath || strings.HasPrefix(rel, "/") || rel == memberPath || strings.HasPrefix(rel, memberPath+"/") {
		return rel
	}
	return memberPath + "/" + rel
}

// rewriteMemberSteps roots a member job's step inputs naming files: the
// setup actions' version files and the PyPI publish action's packages-dir
// (dist/ when unset), which the actions resolve from the repository root
// whatever the working directory.
func rewriteMemberSteps(job *yaml.Node, memberPath, where string) error {
	steps := mapGet(job, "steps")
	if isNull(steps) {
		return nil
	}
	if !isSeq(steps) {
		return fmt.Errorf("%s: steps is not a list (line %d)", where, steps.Line)
	}
	for _, step := range steps.Content {
		if !isMap(step) {
			return fmt.Errorf("%s: a step is not a mapping (line %d)", where, step.Line)
		}
		uses := mapGet(step, "uses")
		usesValue := ""
		if isScalar(uses) {
			usesValue = uses.Value
		}
		with := mapGet(step, "with")
		if strings.Contains(usesValue, "pypa/gh-action-pypi-publish") {
			w, err := mapEnsure(step, "with", where)
			if err != nil {
				return err
			}
			dir := "dist/"
			if v := mapGet(w, "packages-dir"); isScalar(v) && v.Value != "" {
				dir = v.Value
			}
			mapSet(w, "packages-dir", scalar(rootPath(memberPath, dir)))
			continue
		}
		if !isMap(with) {
			continue
		}
		for _, input := range versionFileInputs {
			if v := mapGet(with, input); isScalar(v) {
				mapSet(with, input, scalar(rootPath(memberPath, v.Value)))
			}
		}
	}
	return nil
}

// inheritWorkflowLevel copies the workflow-level env, defaults.run, and
// permissions a job would have inherited into the job, the job's own keys
// winning, since the router keeps the jobs and not their workflow.
func inheritWorkflowLevel(job, workflow *yaml.Node, where string) error {
	if env := mapGet(workflow, "env"); isMap(env) {
		jobEnv, err := mapEnsure(job, "env", where)
		if err != nil {
			return err
		}
		for i := 0; i+1 < len(env.Content); i += 2 {
			if mapGet(jobEnv, env.Content[i].Value) == nil {
				mapSet(jobEnv, env.Content[i].Value, cloneNode(env.Content[i+1]))
			}
		}
	}
	if defaults := mapGet(workflow, "defaults"); isMap(defaults) {
		if run := mapGet(defaults, "run"); isMap(run) {
			jobDefaults, err := mapEnsure(job, "defaults", where)
			if err != nil {
				return err
			}
			jobRun, err := mapEnsure(jobDefaults, "run", where)
			if err != nil {
				return err
			}
			for i := 0; i+1 < len(run.Content); i += 2 {
				if mapGet(jobRun, run.Content[i].Value) == nil {
					mapSet(jobRun, run.Content[i].Value, cloneNode(run.Content[i+1]))
				}
			}
		}
	}
	if perms := mapGet(workflow, "permissions"); perms != nil && mapGet(job, "permissions") == nil {
		mapSet(job, "permissions", cloneNode(perms))
	}
	return nil
}

// setWorkingDirectory sets the job's defaults.run.working-directory to dir
// unless the job sets one.
func setWorkingDirectory(job *yaml.Node, dir, where string) error {
	defaults, err := mapEnsure(job, "defaults", where)
	if err != nil {
		return err
	}
	run, err := mapEnsure(defaults, "run", where)
	if err != nil {
		return err
	}
	if mapGet(run, "working-directory") == nil {
		mapSet(run, "working-directory", scalar(dir))
	}
	return nil
}

// memberCI is one member's CI as the router inlines it.
type memberCI struct {
	member declarations.Member
	keys   []string
	jobs   map[string]*yaml.Node
}

// countReusableCalls counts the jobs calling a reusable workflow.
func countReusableCalls(jobs map[string]*yaml.Node) int {
	n := 0
	for _, j := range jobs {
		if mapGet(j, "uses") != nil {
			n++
		}
	}
	return n
}

// readMemberCI reads and transforms one member's CI sources; nil when it
// has none.
func readMemberCI(w *workspace.Workspace, m declarations.Member, taken map[string]bool) (*memberCI, error) {
	dir := w.MemberDir(m)
	sources, err := CISources(dir)
	if err != nil || len(sources) == 0 {
		return nil, err
	}
	out := &memberCI{member: m, jobs: map[string]*yaml.Node{}}
	cond := "(needs.detect.outputs[" + expressionQuoted(m.Name) + "] == 'true' || inputs." + RunAllInput + ")"
	for _, src := range sources {
		base := filepath.Base(src)
		rel := declarations.Join(declarations.Join(m.Path, Dir), base)
		root, err := readWorkflowSource(src, rel)
		if err != nil {
			return nil, err
		}
		if root == nil {
			return nil, fmt.Errorf("%s has no jobs; a member's CI workflow holds the jobs the router runs for it, so give it its jobs or delete it", rel)
		}
		prefix := JobPrefix(m.Name, base)
		jobs := mapGet(root, "jobs")
		keyMap := map[string]string{}
		for _, k := range mapKeys(jobs) {
			keyMap[k] = prefix + "-" + k
		}
		for _, k := range mapKeys(jobs) {
			j := mapGet(jobs, k)
			where := fmt.Sprintf("%s, job %s", rel, k)
			if !isMap(j) {
				return nil, fmt.Errorf("%s is not a mapping (line %d)", where, j.Line)
			}
			key := keyMap[k]
			if err := validJobKey(key); err != nil {
				return nil, fmt.Errorf("%s: %w", where, err)
			}
			if taken[key] {
				return nil, fmt.Errorf("the CI router job key %q comes from more than one member CI workflow; rename the job %q in %s", key, k, rel)
			}
			taken[key] = true

			display := k
			if name := mapGet(j, "name"); isScalar(name) && name.Value != "" {
				display = name.Value
			}
			mapSet(j, "name", scalar(prefix+" / "+display))

			needs, err := stringsOf(mapGet(j, "needs"), where+": needs")
			if err != nil {
				return nil, err
			}
			rewritten := []string{"detect"}
			for _, n := range needs {
				if mapped, ok := keyMap[n]; ok {
					rewritten = append(rewritten, mapped)
				} else {
					rewritten = append(rewritten, prefix+"-"+n)
				}
			}
			mapSet(j, "needs", sequence(rewritten...))

			condition := cond
			if existing := mapGet(j, "if"); existing != nil {
				if !isScalar(existing) {
					return nil, fmt.Errorf("%s: if is not a single expression (line %d)", where, existing.Line)
				}
				condition = cond + " && (" + stripExpressionWrapper(existing.Value) + ")"
			}
			mapSet(j, "if", scalar(condition))

			// A job calling a reusable workflow takes no env, defaults, or
			// steps; only its permissions are inherited.
			if mapGet(j, "uses") != nil {
				if perms := mapGet(root, "permissions"); perms != nil && mapGet(j, "permissions") == nil {
					mapSet(j, "permissions", cloneNode(perms))
				}
			} else {
				if err := inheritWorkflowLevel(j, root, where); err != nil {
					return nil, err
				}
				if err := setWorkingDirectory(j, m.Path, where); err != nil {
					return nil, err
				}
				if err := rewriteMemberSteps(j, m.Path, where); err != nil {
					return nil, err
				}
			}
			out.keys = append(out.keys, key)
			out.jobs[key] = j
		}
	}
	return out, nil
}

// RouterInputs are what the CI router is rendered from besides the
// workspace.
type RouterInputs struct {
	// Actions pins the detect job's actions (actions/checkout and
	// dorny/paths-filter).
	Actions ActionVersions
}

// CIRouter renders the CI router of the workspace, and false when no member
// has CI of its own, so there is nothing to route.
func CIRouter(w *workspace.Workspace, in RouterInputs) (string, bool, error) {
	if !w.IsWorkspace() {
		return "", false, errors.New("a CI router routes a workspace's members; this repository's declarations declare a standalone layout")
	}
	checkout, err := in.Actions.ref("actions/checkout")
	if err != nil {
		return "", false, err
	}
	pathsFilter, err := in.Actions.ref("dorny/paths-filter")
	if err != nil {
		return "", false, err
	}
	taken := map[string]bool{"detect": true}
	var members []*memberCI
	all := map[string]*yaml.Node{}
	var order []string
	for _, m := range w.Members() {
		ci, err := readMemberCI(w, m, taken)
		if err != nil {
			return "", false, err
		}
		if ci == nil {
			continue
		}
		members = append(members, ci)
		for _, k := range ci.keys {
			all[k] = ci.jobs[k]
			order = append(order, k)
		}
	}
	if len(members) == 0 {
		return "", false, nil
	}
	if n := countReusableCalls(all); n >= maxReusableCalls {
		return "", false, fmt.Errorf("the CI router would call %d reusable workflows (job-level uses), and GitHub refuses a workflow calling %d or more, so it would never run; call fewer from the members' CI workflows", n, maxReusableCalls)
	}
	filters, err := NewFilters(w)
	if err != nil {
		return "", false, err
	}
	var routed []declarations.Member
	for _, ci := range members {
		routed = append(routed, ci.member)
	}
	block, err := filters.Block(routed)
	if err != nil {
		return "", false, err
	}
	jobs, err := encodeJobs(order, all)
	if err != nil {
		return "", false, err
	}

	var b strings.Builder
	b.WriteString(Header + "\n")
	b.WriteString("name: CI Router\n")
	b.WriteString("on:\n  push:\n    branches:\n")
	for _, br := range w.Declarations.ReleaseBranches {
		b.WriteString("      - " + yamlSingleQuoted(br) + "\n")
	}
	b.WriteString("  pull_request:\n")
	b.WriteString("  workflow_dispatch:\n    inputs:\n")
	b.WriteString("      " + RunAllInput + ":\n")
	b.WriteString("        description: Run every member's CI jobs whatever the path filters say, so a release candidate's members run their own CI on a commit whose push touched none of their paths\n")
	b.WriteString("        type: boolean\n        required: false\n        default: false\n")
	// One group per commit and dispatch input: runs of one commit dedupe, a
	// new commit never cancels an earlier one's run, and a run_all dispatch
	// never cancels the push run of the same commit (whose cancellation
	// would read as a red verdict).
	b.WriteString("concurrency:\n")
	b.WriteString("  group: ${{ github.workflow_ref }}-${{ github.sha }}-${{ inputs." + RunAllInput + " }}\n")
	b.WriteString("  cancel-in-progress: true\n")
	b.WriteString("jobs:\n")
	b.WriteString("  detect:\n")
	b.WriteString("    runs-on: ubuntu-latest\n")
	b.WriteString("    outputs:\n")
	for _, ci := range members {
		b.WriteString("      " + ci.member.Name + ": ${{ steps.changes.outputs[" + expressionQuoted(ci.member.Name) + "] }}\n")
	}
	b.WriteString("    steps:\n")
	b.WriteString("      - uses: " + checkout + "\n")
	b.WriteString("      - uses: " + pathsFilter + "\n")
	b.WriteString("        id: changes\n")
	b.WriteString("        with:\n")
	b.WriteString("          filters: |\n")
	b.WriteString(literalBlock(block, "            "))
	b.WriteString("          predicate-quantifier: " + PredicateQuantifier + "\n")
	b.WriteString(jobs)
	return b.String(), true, nil
}
