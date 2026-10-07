package monorepo

import (
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/saferm"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// NoReleasable is the --releasable value that versions a member under no
// releasable, as releasables.toml spells it (releasable = false).
const NoReleasable = "false"

// scaffoldNotCommitted is the line scaffold reports when it does not
// commit.
const scaffoldNotCommitted = "Not committed (--no-auto-commit)."

// AddCommitMessage is the message of the commit `monorepo add` makes for the
// member named name.
func AddCommitMessage(name string) string { return "monorepo: add " + name }

// AddRequest is one `monorepo add`.
type AddRequest struct {
	// Dir is the working directory, absolute; the repository holding it is
	// the one the member is added to.
	Dir string
	// Path is the member's path relative to the repository root, canonical.
	Path string
	// Name is the member's name; empty takes the path's last element.
	Name string
	// Target, when set, is a target the member must have, which scaffold
	// declares when it is not detected; empty requires one to be detected.
	Target    string
	DependsOn []string
	Library   bool
	DevOnly   bool
	// Releasable names the releasable the member is versioned under, or is
	// NoReleasable.
	Releasable string
	// TagFormat and PublishMode declare the releasable the add creates when
	// Releasable names none declared; both are refused otherwise.
	TagFormat   string
	PublishMode string
	// License is the license the releasable the add creates is created
	// under: an SPDX identifier or proprietary. Required when the add
	// creates a releasable, refused otherwise.
	License      string
	RegistryName string
	AutoCommit   bool
	DryRun       bool
	// Version is this rlsbl's version, which scaffold records.
	Version string
	Now     time.Time
	GitHub  github.Client
	Say     func(string)
}

// Add declares a member, creating its releasable when the request names an
// undeclared one, then scaffolds the member (which regenerates the
// workspace's routers) and commits what the three wrote as one commit.
// Every refusal is made before anything is written. When the scaffold, the
// routers, or the commit fails, every path the add changed is put back as
// it was, and nothing is committed. A path that had uncommitted changes
// before the add and is written by it is left uncommitted and named.
func Add(e *strictcli.Effects, req AddRequest) error {
	if req.Say == nil {
		return errors.New("monorepo add needs somewhere to report to")
	}
	ws, err := Load(req.Dir, "add")
	if err != nil {
		return err
	}
	member, created, err := planMember(ws, req)
	if err != nil {
		return err
	}
	data, err := os.ReadFile(filepath.Join(ws.Root, filepath.FromSlash(declarations.ReleasablesFile)))
	if err != nil {
		return fmt.Errorf("reading %s: %w", declarations.ReleasablesFile, err)
	}
	ed, err := declarations.NewEditor(data)
	if err != nil {
		return err
	}
	if created != nil {
		if err := ed.AddReleasable(*created); err != nil {
			return err
		}
	}
	if err := ed.AddMember(member); err != nil {
		return err
	}
	edited, _, err := ed.Result()
	if err != nil {
		return err
	}
	repo, err := git.Open(e, ws.Root)
	if err != nil {
		return err
	}
	record, err := planAddRecord(ws, repo, edited, member, created, req)
	if err != nil {
		return err
	}

	if req.DryRun {
		if _, err := declarations.Write(e, ws.Root, edited); err != nil {
			return err
		}
		req.Say(fmt.Sprintf("Would declare the member %q at %s in %s.", member.Name, member.Path, declarations.ReleasablesFile))
		if created != nil {
			req.Say(fmt.Sprintf("Would declare the releasable %q (tag format %s, publish mode %s).", created.Name, created.TagFormat, created.PublishMode))
			req.Say(fmt.Sprintf("Would record in %s that %q is active and licensed %s from %s, under its releasable name.", lifecycle.RecordFile, created.Name, req.License, req.Now.Format(time.DateOnly)))
		}
		req.Say("Would scaffold the member and regenerate the workspace's routers, then commit what the add wrote as one commit. The scaffold is not previewed: it renders from declarations this preview did not write.")
		return nil
	}

	before, err := snapshotWorkingTree(repo, ws.Root)
	if err != nil {
		return err
	}
	if err := addAndCommit(e, repo, ws.Root, edited, member, created, record, before, req); err != nil {
		if restoreErr := restoreWorkingTree(e, repo, ws.Root, before); restoreErr != nil {
			return fmt.Errorf("%w; putting the working tree back failed too: %v", err, restoreErr)
		}
		return fmt.Errorf("%w; so the member %q is not added: every path the add changed is back as it was before, and nothing was committed. Fix what it reports, then run this `rlsbl monorepo add` again", err, member.Name)
	}
	return nil
}

// addAndCommit writes the declarations, scaffolds the member, and commits.
func addAndCommit(e *strictcli.Effects, repo git.Repo, root string, edited []byte, member declarations.Member, created *declarations.Releasable, record *lifecycle.Record, before snapshot, req AddRequest) error {
	if _, err := declarations.Write(e, root, edited); err != nil {
		return err
	}
	req.Say(fmt.Sprintf("Declared the member %q at %s.", member.Name, member.Path))
	if created != nil {
		req.Say(fmt.Sprintf("Declared the releasable %q (tag format %s, publish mode %s).", created.Name, created.TagFormat, created.PublishMode))
		// The record is written before the scaffold, which renders the
		// member's LICENSE from the license it holds.
		if err := record.Write(recordWriter{e}, root); err != nil {
			return err
		}
		req.Say(fmt.Sprintf("Recorded in %s that %q is active and licensed %s from %s, under its releasable name.", lifecycle.RecordFile, created.Name, req.License, req.Now.Format(time.DateOnly)))
	}
	// The scaffold runs without committing, since the add commits what all
	// of it wrote at once, so its own "not committed" line is not passed on.
	say := func(line string) {
		if line != scaffoldNotCommitted {
			req.Say(line)
		}
	}
	if err := scaffold.Run(e, scaffold.Inputs{
		Dir:     filepath.Join(root, filepath.FromSlash(member.Path)),
		Version: req.Version,
		Target:  req.Target,
		Now:     req.Now,
		Say:     say,
		GitHub:  req.GitHub,
	}); err != nil {
		return fmt.Errorf("scaffolding the member failed: %w", err)
	}
	written, touched, err := changedSince(repo, root, before)
	if err != nil {
		return err
	}
	if len(touched) > 0 {
		req.Say("Left uncommitted, since each had uncommitted changes before this add wrote to it: " + strings.Join(touched, ", "))
	}
	if !req.AutoCommit {
		req.Say("Not committed (--no-auto-commit): " + strings.Join(written, ", "))
		return nil
	}
	if _, err := repo.Commit(git.CommitRequest{Message: AddCommitMessage(member.Name), Paths: written, RequireChange: true}); err != nil {
		return fmt.Errorf("committing what the add wrote failed: %w", err)
	}
	req.Say("Committed: " + AddCommitMessage(member.Name))
	return nil
}

// planAddRecord checks --license against what the add creates and, for a
// releasable it creates, composes the lifecycle-and-license record with
// the releasable's entries, validated against the declarations the add
// writes; nil when it creates none.
func planAddRecord(ws *workspace.Workspace, repo git.Repo, edited []byte, member declarations.Member, created *declarations.Releasable, req AddRequest) (*lifecycle.Record, error) {
	joined := fmt.Sprintf("the member joins the declared releasable %q", member.Releasable)
	if member.Releasable == "" {
		joined = fmt.Sprintf("--releasable %s versions the member under none", NoReleasable)
	}
	if err := requireLicenseForCreated(created != nil, req.License, joined); err != nil {
		return nil, err
	}
	if created == nil {
		return nil, nil
	}
	if err := requirePrivateForProprietary(req.License, req.GitHub, repo, ws.Declarations.GitHubRepository); err != nil {
		return nil, err
	}
	rec, err := lifecycle.Load(ws.Root)
	if err != nil {
		return nil, err
	}
	if err := recordCreatedReleasable(rec, *created, req.License, req.Now, "created by `rlsbl monorepo add`"); err != nil {
		return nil, err
	}
	after, err := declarations.Parse(edited)
	if err != nil {
		return nil, err
	}
	if err := rec.Validate(req.Now, declaredSubjectsOf(after)); err != nil {
		return nil, fmt.Errorf("the lifecycle-and-license record with the releasable %q would be refused, so nothing was written: %w", created.Name, err)
	}
	return rec, nil
}

// planMember checks the request against the workspace and builds the member
// it declares, with the releasable it creates (nil when it joins a declared
// one or none).
func planMember(ws *workspace.Workspace, req AddRequest) (declarations.Member, *declarations.Releasable, error) {
	var none declarations.Member
	if problem := declarations.PathProblem(req.Path); problem != "" {
		return none, nil, errors.New(problem)
	}
	if req.Path == declarations.RootPath {
		return none, nil, fmt.Errorf("the repository root is the root member %q, which every repository declares already; `rlsbl monorepo add` declares the members below it", declarations.RootName)
	}
	if other, ok := ws.Declarations.MemberAt(req.Path); ok {
		return none, nil, fmt.Errorf("the member %q is already declared at %s", other.Name, req.Path)
	}
	dir := filepath.Join(ws.Root, filepath.FromSlash(req.Path))
	info, err := os.Stat(dir)
	if errors.Is(err, os.ErrNotExist) || (err == nil && !info.IsDir()) {
		return none, nil, fmt.Errorf("%s is not a directory under the repository root %s; a member's path names the directory it owns, relative to the root", req.Path, ws.Root)
	}
	if err != nil {
		return none, nil, err
	}

	name := req.Name
	if name == "" {
		name = path.Base(req.Path)
	}
	if problem := declarations.NameProblem(name); problem != "" {
		return none, nil, fmt.Errorf("the member's name: %s", problem)
	}
	if name == declarations.RootName {
		return none, nil, fmt.Errorf("the name %q is reserved for the root member (path \".\"); name the member at %s otherwise with --name", declarations.RootName, req.Path)
	}
	if other, ok := ws.Declarations.Member(name); ok {
		return none, nil, fmt.Errorf("the member at %s is already named %q; name this one otherwise with --name", other.Path, name)
	}

	if req.Target != "" {
		if _, err := targets.Get(req.Target); err != nil {
			return none, nil, fmt.Errorf("--target: %w", err)
		}
	} else {
		found, err := targets.Detect(dir)
		if err != nil {
			return none, nil, err
		}
		if len(found) == 0 {
			return none, nil, fmt.Errorf("no release target is detected in %s: none of its manifests (go.mod, package.json, pyproject.toml with a [project] table) is there. Create the project's manifest first, or name its target with --target", req.Path)
		}
	}

	seen := map[string]bool{}
	for _, dep := range req.DependsOn {
		if seen[dep] {
			return none, nil, fmt.Errorf("--depends-on names %q twice", dep)
		}
		seen[dep] = true
		if _, ok := ws.Declarations.Member(dep); !ok {
			var names []string
			for _, m := range ws.Members() {
				names = append(names, m.Name)
			}
			sort.Strings(names)
			return none, nil, fmt.Errorf("--depends-on names %q, which no member is named; the members are %s", dep, strings.Join(names, ", "))
		}
	}

	m := declarations.Member{
		Path:         req.Path,
		Name:         name,
		DevOnly:      req.DevOnly,
		Library:      req.Library,
		DependsOn:    append([]string(nil), req.DependsOn...),
		RegistryName: req.RegistryName,
	}
	switch req.Releasable {
	case "":
		return none, nil, fmt.Errorf("--releasable is required: name the releasable the member is versioned under, or write --releasable %s for a member versioned under none", NoReleasable)
	case NoReleasable:
		if req.TagFormat != "" || req.PublishMode != "" {
			return none, nil, fmt.Errorf("--tag-format and --publish-mode declare the releasable the add creates, and --releasable %s versions the member under none; drop them, or name the releasable the member belongs to", NoReleasable)
		}
		return m, nil, nil
	}
	m.Releasable = req.Releasable
	if r, ok := ws.Declarations.Releasable(req.Releasable); ok {
		if req.TagFormat != "" || req.PublishMode != "" {
			return none, nil, fmt.Errorf("--tag-format and --publish-mode declare the releasable the add creates, and the releasable %q is declared already (tag format %s, publish mode %s): drop them to version the member under it, or change the releasable in %s", r.Name, r.TagFormat, r.PublishMode, declarations.ReleasablesFile)
		}
		return m, nil, nil
	}
	if problem := declarations.NameProblem(req.Releasable); problem != "" {
		return none, nil, fmt.Errorf("--releasable: %s", problem)
	}
	var missing []string
	if req.TagFormat == "" {
		missing = append(missing, "--tag-format (the releasable's tag scheme, for example \"{name}@v{version}\", or \"v{version}\" for bare version tags)")
	}
	if req.PublishMode == "" {
		missing = append(missing, "--publish-mode (ci publishes from CI, none publishes nothing)")
	}
	if len(missing) > 0 {
		return none, nil, fmt.Errorf("no releasable %q is declared, so the add creates it, and a releasable's tag format and publish mode are stated, never derived: pass %s", req.Releasable, strings.Join(missing, " and "))
	}
	if problem := declarations.TagFormatProblem(req.TagFormat, req.Releasable); problem != "" {
		return none, nil, fmt.Errorf("--tag-format: %s", problem)
	}
	created := &declarations.Releasable{
		Name:        req.Releasable,
		TagFormat:   req.TagFormat,
		PublishMode: declarations.PublishMode(req.PublishMode),
	}
	return m, created, nil
}

// fileState is one path's content as the working tree held it.
type fileState struct {
	exists bool
	data   []byte
}

// snapshot is each path the working tree reports changed, with its content.
type snapshot map[string]fileState

// snapshotWorkingTree records every path git reports changed, untracked
// files one by one, so a later comparison sees each file a step adds inside
// a directory that was already untracked.
func snapshotWorkingTree(repo git.Repo, root string) (snapshot, error) {
	paths, err := repo.ChangedPaths(nil, git.UntrackedAll)
	if err != nil {
		return nil, err
	}
	s := snapshot{}
	for _, p := range paths {
		state, err := readState(root, p)
		if err != nil {
			return nil, err
		}
		s[p] = state
	}
	return s, nil
}

func readState(root, rel string) (fileState, error) {
	data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
	if errors.Is(err, os.ErrNotExist) {
		return fileState{}, nil
	}
	if err != nil {
		return fileState{}, fmt.Errorf("reading %s: %w", rel, err)
	}
	return fileState{exists: true, data: data}, nil
}

// changedSince are the paths changed since before: written were unchanged
// then (a commit may take them), touched had uncommitted changes of their
// own then and were changed again (a commit must not sweep them in).
func changedSince(repo git.Repo, root string, before snapshot) (written, touched []string, err error) {
	now, err := repo.ChangedPaths(nil, git.UntrackedAll)
	if err != nil {
		return nil, nil, err
	}
	for _, p := range now {
		if _, ok := before[p]; !ok {
			written = append(written, p)
		}
	}
	for p, was := range before {
		state, err := readState(root, p)
		if err != nil {
			return nil, nil, err
		}
		if state.exists != was.exists || string(state.data) != string(was.data) {
			touched = append(touched, p)
		}
	}
	sort.Strings(written)
	sort.Strings(touched)
	return written, touched, nil
}

// restoreWorkingTree puts every path changed since before back as it was:
// a path unchanged then gets HEAD's content, or is removed through saferm
// when HEAD has none; a path changed then gets the content it had.
func restoreWorkingTree(e *strictcli.Effects, repo git.Repo, root string, before snapshot) error {
	written, touched, err := changedSince(repo, root, before)
	if err != nil {
		return err
	}
	const reason = "rolling back a monorepo add that failed"
	for _, p := range written {
		content, found, err := repo.FileAt("HEAD", p)
		if err != nil {
			return err
		}
		if found {
			if err := putBack(e, root, p, []byte(content)); err != nil {
				return err
			}
			continue
		}
		if err := saferm.Delete(e, root, saferm.Request{Path: p, Description: reason, SkipMissing: true}); err != nil {
			return err
		}
	}
	for _, p := range touched {
		was := before[p]
		if was.exists {
			if err := putBack(e, root, p, was.data); err != nil {
				return err
			}
			continue
		}
		if err := saferm.Delete(e, root, saferm.Request{Path: p, Description: reason, SkipMissing: true}); err != nil {
			return err
		}
	}
	return nil
}

// putBack writes data at the repository-relative path rel, creating its
// directory.
func putBack(e *strictcli.Effects, root, rel string, data []byte) error {
	target := filepath.Join(root, filepath.FromSlash(rel))
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return fmt.Errorf("creating the directory of %s: %w", rel, err)
	}
	if _, err := e.Write(target, data); err != nil {
		return fmt.Errorf("putting %s back: %w", rel, err)
	}
	return nil
}
