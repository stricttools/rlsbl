package workflows

import (
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The publish router.
//
// A workspace's publish.yml at the root inlines every publishing member's
// publish jobs. It starts when a GitHub Release is published (or is
// dispatched at a tag to publish a release again), and each inlined job runs
// only for its own releasable's tags, read from the tag the dispatch names or
// the ref, never from the release event's payload, which a dispatch has none
// of. One wait-for-ci job holds every publish until the releasing project's
// CI passed on the release commit; a member's own wait-for-ci job is dropped
// when its jobs are inlined.
//
// A member's publish workflow is .github/workflows/publish.yml under its
// directory, rendered there by scaffold. The root member's publish.yml is
// the router itself, so the root member's publish jobs are rendered from
// scaffold's templates (PublishInputs.RootPublishWorkflow) instead of read.

// PublishInputs are what the publish router is rendered from besides the
// workspace.
type PublishInputs struct {
	// RootPublishWorkflow renders the root member's publish workflow from
	// scaffold's templates. It is called only when the root member
	// publishes, and is required then.
	RootPublishWorkflow func(root declarations.Member) (string, error)
}

// PublishPlan is the publish router sync writes, and the member publish
// workflows it removes.
type PublishPlan struct {
	// Text is the publish router; empty when no member publishes.
	Text string
	// Suppressed are the repository-relative publish workflows of members
	// that publish nothing (their releasable's publish mode is none, or they
	// are versioned under none). The router leaves them out, and sync removes
	// them: GitHub never runs a member's own workflow, so its only use is as
	// the router's input, which a member publishing nothing must not be.
	Suppressed []string
}

// publisher is one member whose publish jobs the router inlines.
type publisher struct {
	member  declarations.Member
	tag     TagParts
	pattern string
	root    *yaml.Node
	source  string
}

// memberPublishPath is a member's publish workflow, repository-relative.
func memberPublishPath(m declarations.Member) string {
	return declarations.Join(declarations.Join(m.Path, Dir), PublishFile)
}

// publishesFromCI reports whether the member declares a pipeline publishing
// from CI.
func publishesFromCI(m declarations.Member) bool {
	for _, p := range m.Pipelines {
		if !p.Local {
			return true
		}
	}
	return false
}

// tagOf is the tag scheme of the member's releasable, split at its version.
func tagOf(w *workspace.Workspace, m declarations.Member) (TagParts, error) {
	r, ok := w.ReleasableOf(m)
	if !ok {
		return TagParts{}, fmt.Errorf("the member %q is versioned under no releasable, so it has no tags to publish", m.Name)
	}
	scheme, err := workspace.SchemeOf(r)
	if err != nil {
		return TagParts{}, err
	}
	return TagPartsOf(scheme), nil
}

// publishers finds the members whose publish jobs the router inlines, and
// the publish workflows of members publishing nothing.
func publishers(w *workspace.Workspace, in PublishInputs) ([]publisher, []string, error) {
	var out []publisher
	var suppressed []string
	for _, m := range w.Members() {
		publishes := w.PublishModeOf(m) == declarations.PublishCI
		if m.IsRoot() {
			if !publishes || !publishesFromCI(m) {
				continue
			}
			r, _ := w.ReleasableOf(m)
			if r.PublishCICheckPattern == "" {
				return nil, nil, fmt.Errorf("the root member publishes from CI, and its releasable %q declares no publish_ci_check_pattern: the pattern of the check-run names the root's own CI produces, which wait-for-ci waits for; declare it in %s", r.Name, declarations.ReleasablesFile)
			}
			if in.RootPublishWorkflow == nil {
				return nil, nil, errors.New("the root member publishes from CI, and its publish jobs are rendered from scaffold's templates, which were not given")
			}
			text, err := in.RootPublishWorkflow(m)
			if err != nil {
				return nil, nil, fmt.Errorf("rendering the root member's publish jobs: %w", err)
			}
			source := "the root member's publish workflow rendered from scaffold's templates"
			root, err := parseDocument(text, source)
			if err != nil {
				return nil, nil, err
			}
			if !isMap(root) || !isMap(mapGet(root, "jobs")) {
				return nil, nil, fmt.Errorf("%s has no jobs", source)
			}
			tag, err := tagOf(w, m)
			if err != nil {
				return nil, nil, err
			}
			out = append(out, publisher{member: m, tag: tag, pattern: r.PublishCICheckPattern, root: root, source: source})
			continue
		}
		rel := memberPublishPath(m)
		abs := filepath.Join(w.Root, filepath.FromSlash(rel))
		_, err := os.Stat(abs)
		exists := err == nil
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, nil, err
		}
		if !publishes {
			if exists {
				suppressed = append(suppressed, rel)
			}
			continue
		}
		if !exists {
			if publishesFromCI(m) {
				return nil, nil, fmt.Errorf("the member %q declares a pipeline publishing from CI and has no %s, so no job would publish it; run `rlsbl scaffold` in %s, commit the workflow, and run this again", m.Name, rel, m.Path)
			}
			continue
		}
		root, err := readWorkflowSource(abs, rel)
		if err != nil {
			return nil, nil, err
		}
		if root == nil {
			return nil, nil, fmt.Errorf("%s has no jobs", rel)
		}
		pattern, hasCI, err := CheckPattern(m.Name, w.MemberDir(m))
		if err != nil {
			return nil, nil, err
		}
		if !hasCI {
			return nil, nil, fmt.Errorf("the member %q publishes and has no CI workflow of its own (%s), so wait-for-ci would have no check runs to wait for and would refuse every publish; add the member's CI workflow and run this again", m.Name, declarations.Join(declarations.Join(m.Path, Dir), "ci.yml"))
		}
		tag, err := tagOf(w, m)
		if err != nil {
			return nil, nil, err
		}
		out = append(out, publisher{member: m, tag: tag, pattern: pattern, root: root, source: rel})
	}
	return out, suppressed, nil
}

// tagCondition is the expression true when the router's wait-for-ci job
// judged the tag being published (the dispatch's tag input or the ref) a
// tag of the scheme.
func tagCondition(tag TagParts) string {
	return "needs." + WaitForCIJobKey + ".outputs." + RouterTagSchemeOutput + " == " + expressionQuoted(tag.Pattern())
}

// checksOut reports whether one of the job's steps checks the repository
// out with actions/checkout.
func checksOut(job *yaml.Node) bool {
	steps := mapGet(job, "steps")
	if !isSeq(steps) {
		return false
	}
	for _, step := range steps.Content {
		if uses := mapGet(step, "uses"); isScalar(uses) && strings.HasPrefix(uses.Value, "actions/checkout@") {
			return true
		}
	}
	return false
}

// composeWorkingDirectory roots a job's working directory under the
// member's path: the member's path when the job sets none, the two joined
// when it sets one relative to the member.
func composeWorkingDirectory(job *yaml.Node, memberPath, where string) error {
	defaults, err := mapEnsure(job, "defaults", where)
	if err != nil {
		return err
	}
	run, err := mapEnsure(defaults, "run", where)
	if err != nil {
		return err
	}
	dir := memberPath
	if v := mapGet(run, "working-directory"); isScalar(v) && v.Value != "" && v.Value != "." {
		dir = path.Clean(path.Join(memberPath, v.Value))
	}
	mapSet(run, "working-directory", scalar(dir))
	return nil
}

// inlinePublishJobs transforms one publisher's jobs for the router: its own
// wait-for-ci dropped, the workflow-level env, defaults.run, and permissions
// pushed down, file inputs rooted under the member, the working directory
// too for a job that checks the repository out, each job run only for the
// member's tags (as the router's wait-for-ci job judged the tag), keys prefixed with the member's name, and every job needing the
// router's wait-for-ci.
func inlinePublishJobs(p publisher, taken map[string]bool) ([]string, map[string]*yaml.Node, error) {
	jobs := mapGet(p.root, "jobs")
	if mapGet(jobs, RetiredWaitJobKey) != nil {
		return nil, nil, fmt.Errorf("%s has a job named %q, the waiting job's name before it was renamed %s; run `rlsbl scaffold` in %s to render the current publish workflow, commit it, and run this again", p.source, RetiredWaitJobKey, WaitForCIJobKey, p.member.Path)
	}
	var keys []string
	for _, k := range mapKeys(jobs) {
		if k != WaitForCIJobKey {
			keys = append(keys, k)
		}
	}
	keyMap := map[string]string{WaitForCIJobKey: WaitForCIJobKey}
	for _, k := range keys {
		keyMap[k] = p.member.Name + "-" + k
	}
	cond := tagCondition(p.tag)
	out := map[string]*yaml.Node{}
	var order []string
	for _, k := range keys {
		j := mapGet(jobs, k)
		where := fmt.Sprintf("%s, job %s", p.source, k)
		if !isMap(j) {
			return nil, nil, fmt.Errorf("%s is not a mapping (line %d)", where, j.Line)
		}
		key := keyMap[k]
		if err := validJobKey(key); err != nil {
			return nil, nil, fmt.Errorf("%s: %w", where, err)
		}
		if taken[key] {
			return nil, nil, fmt.Errorf("the publish router job key %q comes from more than one member publish workflow; rename the job %q in %s", key, k, p.source)
		}
		taken[key] = true
		if mapGet(j, "uses") != nil {
			if perms := mapGet(p.root, "permissions"); perms != nil && mapGet(j, "permissions") == nil {
				mapSet(j, "permissions", cloneNode(perms))
			}
		} else {
			if err := inheritWorkflowLevel(j, p.root, where); err != nil {
				return nil, nil, err
			}
			if err := rewriteMemberSteps(j, p.member.Path, where); err != nil {
				return nil, nil, err
			}
			// A job that checks no repository out (the npm per-platform jobs
			// work in $RUNNER_TEMP) has no member directory on its runner.
			if checksOut(j) {
				if err := composeWorkingDirectory(j, p.member.Path, where); err != nil {
					return nil, nil, err
				}
			}
		}
		condition := cond
		if existing := mapGet(j, "if"); existing != nil {
			if !isScalar(existing) {
				return nil, nil, fmt.Errorf("%s: if is not a single expression (line %d)", where, existing.Line)
			}
			condition = cond + " && (" + stripExpressionWrapper(existing.Value) + ")"
		}
		mapSet(j, "if", scalar(condition))
		needs, err := stringsOf(mapGet(j, "needs"), where+": needs")
		if err != nil {
			return nil, nil, err
		}
		rewritten := []string{WaitForCIJobKey}
		for _, n := range needs {
			if n == WaitForCIJobKey {
				continue
			}
			if mapped, ok := keyMap[n]; ok {
				rewritten = append(rewritten, mapped)
			} else {
				rewritten = append(rewritten, p.member.Name+"-"+n)
			}
		}
		mapSet(j, "needs", sequence(rewritten...))
		out[key] = j
		order = append(order, key)
	}
	return order, out, nil
}

// PublishRouter renders the workspace's publish router, and lists the
// publish workflows of members publishing nothing. No publishing member
// renders no router.
func PublishRouter(w *workspace.Workspace, in PublishInputs) (PublishPlan, error) {
	if !w.IsWorkspace() {
		return PublishPlan{}, errors.New("a publish router publishes a workspace's members; this repository's declarations declare a standalone layout")
	}
	pubs, suppressed, err := publishers(w, in)
	if err != nil {
		return PublishPlan{}, err
	}
	plan := PublishPlan{Suppressed: suppressed}
	if len(pubs) == 0 {
		return plan, nil
	}
	var projects []ReleasingProject
	for _, p := range pubs {
		projects = append(projects, ReleasingProject{Tag: p.tag, CheckPattern: p.pattern})
	}
	wait, err := RouterWaitForCIJob(projects)
	if err != nil {
		return PublishPlan{}, err
	}
	taken := map[string]bool{WaitForCIJobKey: true}
	all := map[string]*yaml.Node{}
	var order []string
	for _, p := range pubs {
		keys, jobs, err := inlinePublishJobs(p, taken)
		if err != nil {
			return PublishPlan{}, err
		}
		for _, k := range keys {
			all[k] = jobs[k]
			order = append(order, k)
		}
	}
	if n := countReusableCalls(all); n >= maxReusableCalls {
		return PublishPlan{}, fmt.Errorf("the publish router would call %d reusable workflows (job-level uses), and GitHub refuses a workflow calling %d or more, so it would never run; call fewer from the members' publish workflows", n, maxReusableCalls)
	}
	jobs, err := encodeJobs(order, all)
	if err != nil {
		return PublishPlan{}, err
	}
	var b strings.Builder
	b.WriteString(Header + "\n")
	b.WriteString("# To publish a release again, dispatch this workflow at the tag:\n")
	b.WriteString("# gh workflow run publish.yml --ref <tag>. The jobs and wait-for-ci pick the\n")
	b.WriteString("# releasing project from the tag, never from the release event's payload,\n")
	b.WriteString("# which a dispatch has none of.\n")
	b.WriteString("name: Publish Router\n")
	b.WriteString("on:\n  release:\n    types: [published]\n")
	b.WriteString("  workflow_dispatch:\n    inputs:\n      tag:\n")
	b.WriteString("        description: The release tag to publish, overriding the ref when a release is published again\n")
	b.WriteString("        required: false\n        type: string\n")
	b.WriteString(PublishConcurrency)
	b.WriteString("jobs:\n")
	b.WriteString(wait)
	b.WriteString(jobs)
	plan.Text = b.String()
	return plan, nil
}
