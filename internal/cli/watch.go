package cli

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/ci"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/workspace"
)

var watchHelp = fmt.Sprintf("Watch the GitHub Actions runs of a commit (HEAD when none is named), or of the runs --run-id names, until each concludes, "+
	"through the authenticated gh command line. A run gh stops watching without a pass is a failure only when the run's own state says it "+
	"completed; a run still going is watched again. A failed run is classified from its failing jobs' logs: a deterministic failure (tests, "+
	"compilation, configuration, workflow syntax, a missing secret) is reported with the command running its failed jobs by hand, and any "+
	"other failure is run again once (only its failed jobs for an infrastructure failure, where the run never executed). When the commit is "+
	"a release commit of a releasable the release record holds, every release-triggered workflow of the tagged tree must show a run for "+
	"its tag, or the command fails naming what it found. Runs started after the first listing are watched too. A commit on which no run "+
	"appears within %d seconds fails, naming the command to run again. Exits 0 when every run passed and 1 otherwise.", int64(ci.WatchDiscovery/time.Second))

// watchPayload is watch's payload.
type watchPayload struct {
	Commit   string         `json:"commit"`
	Releases []string       `json:"releases"`
	Runs     []ci.RunResult `json:"runs"`
	Passed   bool           `json:"passed"`
}

func registerWatch(r *commandSet) {
	r.add(command{
		path:   []string{"watch"},
		help:   watchHelp,
		effect: mutating,
		args: []strictcli.Arg{
			strictcli.NewArg("sha", "The commit whose runs are watched, by hash or any revision git resolves; HEAD when neither it nor --run-id is given", strictcli.ArgOptional()),
		},
		flags: []strictcli.Flag{
			strictcli.StringFlag("run-id", "The id of one workflow run to watch instead of a commit's runs; repeatable", strictcli.Optional(), strictcli.Repeatable(), strictcli.Unique(true)),
		},
		payload: watchPayloadSchema(),
		render:  renderWatch,
		run:     runWatch,
	})
}

// watchContext is the repository a watch runs in and the GitHub repository
// it watches.
type watchContext struct {
	repo git.Repo
	w    *workspace.Workspace
	gh   github.Client
	slug github.Repository
}

func openWatchContext(ctx *strictcli.Context) (watchContext, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return watchContext{}, err
	}
	root, err := declarations.FindRepositoryRoot(cwd)
	if err != nil {
		return watchContext{}, err
	}
	repo, err := git.Open(ctx.Effects(), root)
	if err != nil {
		return watchContext{}, err
	}
	wc := watchContext{repo: repo}
	declared := ""
	if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(declarations.ReleasablesFile))); err == nil {
		if wc.w, err = workspace.Load(root); err != nil {
			return watchContext{}, err
		}
		declared = wc.w.Declarations.GitHubRepository
	} else if !errors.Is(err, os.ErrNotExist) {
		return watchContext{}, err
	}
	origin := ""
	configured, err := repo.RemoteConfigured("origin")
	if err != nil {
		return watchContext{}, err
	}
	if configured {
		if origin, err = repo.RemoteURL("origin"); err != nil {
			return watchContext{}, err
		}
	}
	if wc.slug, err = github.ResolveRepository(declared, origin); err != nil {
		return watchContext{}, err
	}
	if wc.gh, err = github.New(ctx.Effects()); err != nil {
		return watchContext{}, err
	}
	return wc, nil
}

// releasesAt are the tags of the releases the commit is, across every
// releasable the repository declares; none outside an rlsbl repository.
func (wc watchContext) releasesAt(commit string) ([]string, error) {
	if wc.w == nil {
		return nil, nil
	}
	var tags []string
	for _, r := range wc.w.Releasables() {
		scheme, err := workspace.SchemeOf(r)
		if err != nil {
			return nil, err
		}
		entry, err := releaserecord.New(wc.repo, r.Name, scheme, "").AtCommit(commit)
		if err != nil {
			return nil, err
		}
		if entry != nil {
			tags = append(tags, entry.Tag(scheme))
		}
	}
	return tags, nil
}

func runWatch(ctx *strictcli.Context, kw map[string]any) (any, error) {
	commitArg, hasCommit := strictcli.GetOpt[string](kw, "sha")
	var ids []int64
	if raw, ok := kw["run_id"]; ok && raw != nil {
		texts, err := stringList(raw)
		if err != nil {
			return nil, err
		}
		for _, t := range texts {
			id, err := strconv.ParseInt(t, 10, 64)
			if err != nil || id <= 0 {
				return nil, fmt.Errorf("--run-id takes a workflow run's numeric id, and %q is not one", t)
			}
			ids = append(ids, id)
		}
	}
	if hasCommit && len(ids) > 0 {
		return nil, errors.New("name a commit or --run-id, not both: a commit's runs are listed, and --run-id names the runs")
	}
	wc, err := openWatchContext(ctx)
	if err != nil {
		return nil, err
	}
	watcher := ci.Watcher{GH: wc.gh, Repo: wc.slug, Log: ctx.Info, Sleep: time.Sleep, Now: time.Now}
	payload := watchPayload{Releases: []string{}, Runs: []ci.RunResult{}}
	var results []ci.RunResult
	if len(ids) > 0 {
		var labels []string
		for _, id := range ids {
			labels = append(labels, strconv.FormatInt(id, 10))
		}
		results, err = watcher.WatchRunIDs(ids, "runs "+strings.Join(labels, ","))
		if err != nil {
			return nil, err
		}
	} else {
		rev := "HEAD"
		if hasCommit {
			rev = commitArg
		}
		sha, found, err := wc.repo.ResolveCommit(rev)
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, fmt.Errorf("%q names no commit in %s", rev, wc.repo.Dir())
		}
		payload.Commit = sha
		tags, err := wc.releasesAt(sha)
		if err != nil {
			return nil, err
		}
		payload.Releases = append(payload.Releases, tags...)
		label := sha[:12]
		if len(tags) > 0 {
			label = strings.Join(tags, ", ")
		}
		// A Release that started no publish run says so nowhere, so a
		// released commit's publish runs are confirmed before its runs are
		// watched.
		runs := ci.PublishRuns{GH: wc.gh, Repo: wc.slug, Git: wc.repo, Log: ctx.Info, Sleep: time.Sleep, Now: time.Now}
		for _, tag := range tags {
			if err := runs.ConfirmRunsStarted(tag, sha, ci.ConfirmDiscovery, ci.ConfirmInterval); err != nil {
				return payload, err
			}
		}
		results, err = watcher.WatchCommit(sha, label)
		if err != nil {
			payload.Runs = append(payload.Runs, results...)
			return payload, err
		}
	}
	payload.Runs = append(payload.Runs, results...)
	payload.Passed = len(results) > 0
	for _, r := range results {
		if !r.Passed {
			payload.Passed = false
		}
	}
	if !payload.Passed {
		return payload, &exitStatus{code: 1}
	}
	return payload, nil
}

func watchPayloadSchema() map[string]any {
	str := map[string]any{"type": "string"}
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"commit":   str,
			"releases": map[string]any{"type": "array", "items": str},
			"runs": map[string]any{"type": "array", "items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"name":       str,
					"run_id":     map[string]any{"type": "integer"},
					"passed":     map[string]any{"type": "boolean"},
					"unresolved": map[string]any{"type": "boolean"},
				},
				"required":             []any{"name", "run_id", "passed", "unresolved"},
				"additionalProperties": false,
			}},
			"passed": map[string]any{"type": "boolean"},
		},
		"required":             []any{"commit", "releases", "runs", "passed"},
		"additionalProperties": false,
	}
}

func renderWatch(payload any) string {
	p, err := decodePayload[watchPayload](payload)
	if err != nil {
		return renderJSON(payload)
	}
	rows := make([][]string, 0, len(p.Runs))
	for _, r := range p.Runs {
		state := "passed"
		switch {
		case r.Unresolved:
			state = "unresolved"
		case !r.Passed:
			state = "FAILED"
		}
		rows = append(rows, []string{r.Name, strconv.FormatInt(r.RunID, 10), state})
	}
	head := "Runs"
	if p.Commit != "" {
		head = "Runs on " + p.Commit
	}
	if len(p.Releases) > 0 {
		head += " (" + strings.Join(p.Releases, ", ") + ")"
	}
	return head + "\n\n" + renderTable([]string{"workflow", "run", "result"}, rows)
}
