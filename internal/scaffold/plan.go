package scaffold

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
)

// Every file scaffold renders is planned before anything is written. A file
// that does not exist is created. An existing one is merged three ways: the
// stored merge base (the template text it was last rendered from), the file
// as it stands (ours), and the template's text now (theirs), so a local edit
// and a template change both remain. A file with no stored base has one
// rebuilt from its last `rlsbl scaffold` commit; a file with neither is
// refused unless it already equals the template. A conflict is written with
// git's markers and refuses the run once everything else is written, naming
// the lines.

// commitMessage is the message of every scaffold commit; a missing merge
// base is rebuilt from the last commit carrying it.
const commitMessage = "rlsbl scaffold"

// The statuses a file's row reports.
const (
	statusCreated    = "created"
	statusUpdated    = "updated"
	statusMerged     = "merged"
	statusUnchanged  = "unchanged"
	statusSeeded     = "unchanged, base seeded"
	statusHealed     = "unchanged, base healed"
	statusUserOwned  = "user-owned"
	statusLinesAdded = "updated (lines added)"
	statusConflicts  = "CONFLICTS: resolve manually"
	statusRemoved    = "removed"
)

// filePlan is what scaffold does to one file.
type filePlan struct {
	path   string
	status string
	// write: content replaces the file.
	write   bool
	content string
	// base, when writeBase, is stored as the file's merge base.
	writeBase bool
	base      string
	// record: the file is managed, and its hash after the run is hash.
	record bool
	hash   string
	// chmod: the file exists unchanged but is not executable.
	chmod      bool
	executable bool
	// conflicts are the merge's conflict regions.
	conflicts []ConflictRegion
	// healed names the commit a missing merge base was rebuilt from.
	healed string
	shared bool
	// uncommittedBase: the stored merge base differs from the one HEAD holds,
	// so an earlier run wrote it and left it uncommitted (the run that wrote
	// a conflict, or one under --no-auto-commit); this run commits the file
	// and its base.
	uncommittedBase bool
}

// changed reports whether the run writes something for the file.
func (p filePlan) changed() bool { return p.write || p.writeBase || p.chmod }

// lastScaffoldBase rebuilds the merge base of path from its most recent
// `rlsbl scaffold` commit; found is false when no such commit touched it.
func lastScaffoldBase(repo git.Repo, r git.Runner, path string) (base, commit string, found bool, err error) {
	out, ok, err := gitAnswer(r, repo.Dir(), "log", "--grep="+commitMessage, "-n1", "--format=%H", "--", path)
	if err != nil {
		return "", "", false, err
	}
	if !ok || out == "" {
		return "", "", false, nil
	}
	content, found, err := repo.FileAt(out, path)
	if err != nil || !found {
		return "", "", false, err
	}
	return content, out[:9], true, nil
}

// planFile plans one rendered file.
func planFile(root string, repo git.Repo, r git.Runner, f render) (filePlan, error) {
	plan := filePlan{path: f.path, executable: f.executable, shared: f.shared}
	abs := filepath.Join(root, filepath.FromSlash(f.path))
	data, err := os.ReadFile(abs)
	exists := err == nil
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return filePlan{}, fmt.Errorf("reading %s: %w", f.path, err)
	}
	ours := string(data)
	switch {
	case f.userOwned && exists:
		plan.status = statusUserOwned
		return plan, nil
	case f.userOwned:
		plan.status, plan.write, plan.content = statusCreated, true, f.theirs
		return plan, nil
	case f.mergeLines && exists:
		merged := mergeLines(ours, f.theirs)
		if merged == ours {
			plan.status = statusUnchanged
		} else {
			plan.status, plan.write, plan.content = statusLinesAdded, true, merged
		}
		return plan, nil
	case f.mergeLines:
		plan.status, plan.write, plan.content = statusCreated, true, f.theirs
		return plan, nil
	case !exists:
		plan.status, plan.write, plan.content = statusCreated, true, f.theirs
		plan.writeBase, plan.base = true, f.theirs
		plan.record, plan.hash = true, FileHash([]byte(f.theirs))
		return plan, nil
	}
	plan.record = true
	base, found, err := readBase(root, f.path)
	if err != nil {
		return filePlan{}, err
	}
	if !found {
		rebuilt, commit, ok, err := lastScaffoldBase(repo, r, f.path)
		if err != nil {
			return filePlan{}, err
		}
		switch {
		case ok:
			base, plan.healed = rebuilt, commit
		case ours == f.theirs:
			plan.status, plan.writeBase, plan.base = statusSeeded, true, f.theirs
			plan.hash = FileHash(data)
			return withMode(plan, abs)
		default:
			return filePlan{}, fmt.Errorf("%s: cannot merge template updates: there is no stored merge base (%s) and no `rlsbl scaffold` commit in git history to rebuild one from. Either delete %s and run rlsbl scaffold again (it creates a missing file from the template), or, only when the file is unmodified template output, commit it with a message containing %q so that commit becomes its base; for a file holding your own changes that second way makes the next scaffold replace them with the template", f.path, BasePath(f.path), f.path, commitMessage)
		}
	}
	if found {
		committed, inHead, err := repo.FileAt("HEAD", BasePath(f.path))
		if err != nil {
			return filePlan{}, err
		}
		plan.uncommittedBase = !inHead || committed != base
	}
	healedStatus := statusUnchanged
	if plan.healed != "" {
		healedStatus = statusHealed
		plan.writeBase, plan.base = true, f.theirs
	}
	switch {
	case ours == f.theirs, base == f.theirs:
		plan.status = healedStatus
		plan.hash = FileHash(data)
		return withMode(plan, abs)
	case ours == base:
		plan.status, plan.write, plan.content = statusUpdated, true, f.theirs
	default:
		merged, conflicts, err := repo.ThreeWayMerge(ours, base, f.theirs)
		if err != nil {
			return filePlan{}, err
		}
		plan.write, plan.content = true, merged
		plan.status = statusMerged
		if conflicts > 0 {
			plan.status = statusConflicts
			plan.conflicts = ConflictRegions(merged)
		}
	}
	plan.writeBase, plan.base = true, f.theirs
	plan.hash = FileHash([]byte(plan.content))
	return plan, nil
}

// withMode plans making an executable file executable when it is not.
func withMode(plan filePlan, abs string) (filePlan, error) {
	if !plan.executable {
		return plan, nil
	}
	info, err := os.Stat(abs)
	if err != nil {
		return filePlan{}, err
	}
	plan.chmod = info.Mode().Perm()&0o111 == 0
	return plan, nil
}

// mergeLines adds to text every non-blank, non-comment line of template it
// lacks (compared without surrounding whitespace), after a blank line, and
// removes none.
func mergeLines(text, template string) string {
	have := map[string]bool{}
	for _, line := range strings.Split(text, "\n") {
		if s := strings.TrimSpace(line); s != "" && !strings.HasPrefix(s, "#") {
			have[s] = true
		}
	}
	var missing []string
	for _, line := range strings.Split(template, "\n") {
		s := strings.TrimSpace(line)
		if s != "" && !strings.HasPrefix(s, "#") && !have[s] {
			have[s] = true
			missing = append(missing, line)
		}
	}
	if len(missing) == 0 {
		return text
	}
	return strings.TrimRight(text, "\n") + "\n\n" + strings.Join(missing, "\n") + "\n"
}

// applyPlan performs one file's plan through the effects handle.
func applyPlan(e *strictcli.Effects, root string, p filePlan) error {
	abs := filepath.Join(root, filepath.FromSlash(p.path))
	if p.write {
		if _, err := e.Mkdir(filepath.Dir(abs)); err != nil {
			return fmt.Errorf("creating the directory of %s: %w", p.path, err)
		}
		var opts []strictcli.EffectOption
		if p.executable {
			opts = append(opts, strictcli.Mode(0o755))
		}
		if _, err := e.Write(abs, p.content, opts...); err != nil {
			return fmt.Errorf("writing %s: %w", p.path, err)
		}
	}
	if p.chmod {
		if _, err := e.Chmod(abs, 0o755); err != nil {
			return fmt.Errorf("making %s executable: %w", p.path, err)
		}
	}
	if p.writeBase {
		if err := writeBase(e, root, p.path, p.base); err != nil {
			return err
		}
	}
	return nil
}
