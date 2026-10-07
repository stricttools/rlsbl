package publishrules

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/git"
)

// WorkflowsDir is where GitHub reads a repository's workflows from,
// relative to the repository root.
const WorkflowsDir = ".github/workflows"

// pypiPublishAction is the action publishing to PyPI, which attaches
// attestations unless told not to.
const pypiPublishAction = "pypa/gh-action-pypi-publish"

// WorkflowFile is one workflow: its repository-relative path and its text.
type WorkflowFile struct {
	Path string
	Text string
}

// CommittedWorkflows are the workflows in rev's tree. What a release
// publishes is the committed tree it tags, never the working tree: a
// workflow fixed but not committed still runs as committed.
func CommittedWorkflows(repo git.Repo, rev string) ([]WorkflowFile, error) {
	paths, err := repo.FilesAt(rev, WorkflowsDir)
	if err != nil {
		return nil, err
	}
	var files []WorkflowFile
	for _, p := range paths {
		if !isWorkflow(p) {
			continue
		}
		text, found, err := repo.FileAt(rev, p)
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, fmt.Errorf("%s lists %s, and reading it found nothing", rev, p)
		}
		files = append(files, WorkflowFile{Path: p, Text: text})
	}
	return files, nil
}

// WorkingTreeWorkflows are the workflows under root's .github/workflows as
// they are on disk; none when the directory does not exist.
func WorkingTreeWorkflows(root string) ([]WorkflowFile, error) {
	dir := filepath.Join(root, filepath.FromSlash(WorkflowsDir))
	var files []WorkflowFile
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			if p == dir && errors.Is(err, fs.ErrNotExist) {
				return fs.SkipAll
			}
			return err
		}
		if d.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if !isWorkflow(rel) {
			return nil
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		files = append(files, WorkflowFile{Path: rel, Text: string(data)})
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	return files, nil
}

func isWorkflow(p string) bool {
	ext := path.Ext(p)
	return ext == ".yml" || ext == ".yaml"
}

// workflow is the part of a workflow document the findings read.
type workflow struct {
	Jobs map[string]struct {
		Steps []map[string]any `yaml:"steps"`
	} `yaml:"jobs"`
}

// workflowFinding is one step shape that publishes something the
// private-repository-publishing rule judges, and how to stop it.
type workflowFinding struct {
	finds  func(step map[string]any) bool
	output lifecycle.Output
	what   string
}

var workflowFindings = []workflowFinding{
	{attests, lifecycle.BuildAttestation, "publishes to PyPI with attestations, which record the repository's name, workflow, and commit in a public transparency log; regenerate the workflow with `rlsbl scaffold`, which renders attestations: false for a repository that may not attest, and commit it"},
	{asksModuleProxy, lifecycle.GoProxyNotification, "asks the Go module proxy for the released module; regenerate the workflow with `rlsbl scaffold`, which renders no proxy request for a repository that may not make one, and commit it"},
	{requestsProvenance, lifecycle.BuildAttestation, "publishes to npm with --provenance; regenerate the workflow with `rlsbl scaffold`, which renders no --provenance for a repository that may not attest, and commit it"},
}

func stepString(step map[string]any, key string) string {
	s, _ := step[key].(string)
	return s
}

// attests reports whether the step publishes to PyPI with attestations on,
// which the action does unless attestations is false.
func attests(step map[string]any) bool {
	if !strings.HasPrefix(stepString(step, "uses"), pypiPublishAction) {
		return false
	}
	with, _ := step["with"].(map[string]any)
	switch v := with["attestations"].(type) {
	case bool:
		return v
	case string:
		return strings.ToLower(strings.TrimSpace(v)) != "false"
	}
	return true
}

// asksModuleProxy reports whether the step asks the Go module proxy for a
// module version: `go list -m` against proxy.golang.org. Downloading
// dependencies through the proxy asks it for public modules only, and is
// not this.
func asksModuleProxy(step map[string]any) bool {
	run := stepString(step, "run")
	return strings.Contains(run, "proxy.golang.org") && strings.Contains(run, "go list -m")
}

// requestsProvenance reports whether the step publishes with build
// provenance.
func requestsProvenance(step map[string]any) bool {
	run := stepString(step, "run")
	return strings.Contains(run, "--provenance") && strings.Contains(run, "publish")
}

// WorkflowUses are the outputs the workflows' publish steps produce, one per
// finding per job. They are outputs of the repository as a whole (no
// subject), judged by the private-repository-publishing rule. A workflow
// that is not YAML is an error naming it.
func WorkflowUses(files []WorkflowFile) ([]Use, error) {
	var uses []Use
	for _, f := range files {
		var doc workflow
		if err := yaml.Unmarshal([]byte(f.Text), &doc); err != nil {
			return nil, fmt.Errorf("%s is not a workflow rlsbl can read: %w", f.Path, err)
		}
		jobs := make([]string, 0, len(doc.Jobs))
		for name := range doc.Jobs {
			jobs = append(jobs, name)
		}
		sort.Strings(jobs)
		for _, name := range jobs {
			for _, finding := range workflowFindings {
				for _, step := range doc.Jobs[name].Steps {
					if finding.finds(step) {
						uses = append(uses, Use{Output: finding.output, Where: fmt.Sprintf("%s (job %s) %s", f.Path, name, finding.what)})
						break
					}
				}
			}
		}
	}
	return uses, nil
}
