package lifecycleops

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/monorepo"
)

// retireReleasables finishes `transition lifecycle --status retired` for
// declared releasables, whose retired periods the record rec already holds,
// opening on the date of on: the releasables and their members leave the
// declarations together (monorepo.PlanRetirement), the open identities of
// every one of them close on that date, so the tags they owned are
// accounted for by the record, and the record, the declarations, and what
// the retirement regenerates are committed as one commit. Their changelog
// and release directories stay where they are.
func (inv Invocation) retireReleasables(m managed, rec *lifecycle.Record, names []string, on time.Time) error {
	quoted := make([]string, len(names))
	for i, n := range names {
		quoted[i] = fmt.Sprintf("%q", n)
	}
	what := monorepo.JoinWords(quoted)
	if inv.RouterInputs == nil {
		return fmt.Errorf("retiring %s regenerates the routers, and this invocation was given no router inputs", what)
	}
	plan, err := monorepo.PlanRetirement(m.ws, names, inv.RouterInputs)
	if err != nil {
		return err
	}
	touched := append(append([]string(nil), recordPaths...), monorepo.RetireTouches...)
	dirty, err := m.repo.ChangedPaths(touched, git.UntrackedNormal)
	if err != nil {
		return err
	}
	if len(dirty) > 0 {
		return fmt.Errorf("retiring %s writes and deletes paths that have uncommitted changes, which its commit would carry; commit or revert them first, then run this again; nothing was written:\n  %s", what, strings.Join(dirty, "\n  "))
	}
	subjects := plan.Subjects()
	for _, subject := range subjects[len(names):] {
		if err := closeOpen(rec, lifecycle.TableLifecycle, subject, on); err != nil {
			return err
		}
	}
	wanted := map[string]bool{}
	for _, s := range subjects {
		wanted[s] = true
	}
	for _, id := range rec.Identities() {
		if wanted[id.Subject] && !id.Pending() && id.Open() {
			if err := rec.CloseIdentity(id.Subject, id.Facet, id.Registry, on); err != nil {
				return err
			}
		}
	}
	if err := rec.Validate(on, plan.DeclaredSubjects()); err != nil {
		return fmt.Errorf("the record would not be valid once %s %s the declarations, so nothing was written: %w", what, leaveVerb(len(names)), err)
	}
	if err := rec.Write(recordWriter{inv.E}, m.root); err != nil {
		return err
	}
	res, err := plan.Apply(inv.E)
	if err != nil {
		return fmt.Errorf("retiring %s stopped part-way, and nothing was committed; the working tree holds what was written (`git status` lists it): %w", what, err)
	}
	var memberNames []string
	for _, mem := range plan.Members {
		memberNames = append(memberNames, fmt.Sprintf("%s (%s)", mem.Name, mem.Path))
	}
	removed := ""
	if len(res.Removed) > 0 {
		removed = "; removed " + strings.Join(res.Removed, ", ")
	}
	plain := monorepo.JoinWords(names)
	members := strings.Join(memberNames, ", ")
	if len(names) == 1 {
		inv.report(
			fmt.Sprintf("%s is retired from %s: it left the declarations with its members %s, its open identities closed%s. Its changelog and release history stay as they are.", plain, day(on), members, removed),
			fmt.Sprintf("Would record %s as retired from %s, take it out of the declarations with its members %s, and close its open identities%s.", plain, day(on), members, strings.Replace(removed, "removed", "remove", 1)),
		)
	} else {
		inv.report(
			fmt.Sprintf("%s are retired from %s: they left the declarations with their members %s, their open identities closed%s. Their changelog and release history stay as they are.", plain, day(on), members, removed),
			fmt.Sprintf("Would record %s as retired from %s, take them out of the declarations with their members %s, and close their open identities%s.", plain, day(on), members, strings.Replace(removed, "removed", "remove", 1)),
		)
	}
	// The paths under touched are committed as git reports them, file by
	// file, deletions included; a removal sync made elsewhere (a member's
	// own publish workflow) is named as sync reports it.
	paths := append([]string(nil), recordPaths...)
	for _, p := range append(append([]string(nil), res.Written...), res.Removed...) {
		if inv.DryRun || !insideAny(p, touched) {
			paths = append(paths, p)
		}
	}
	if !inv.DryRun {
		changed, err := m.repo.ChangedPaths(touched, git.UntrackedNormal)
		if err != nil {
			return err
		}
		paths = append(paths, changed...)
	}
	return inv.commit(m.repo, fmt.Sprintf("transition: %s %s %s", plain, isAre(len(names)), lifecycle.StatusRetired), dedupe(paths))
}

// isAre is the verb "is" for one subject and "are" for more.
func isAre(n int) string {
	if n == 1 {
		return "is"
	}
	return "are"
}

// leaveVerb is the verb "leaves" for one subject and "leave" for more.
func leaveVerb(n int) string {
	if n == 1 {
		return "leaves"
	}
	return "leave"
}

// insideAny reports whether the repository-relative path p is one of dirs
// or lies inside one.
func insideAny(p string, dirs []string) bool {
	for _, d := range dirs {
		if declarations.IsInside(p, d) {
			return true
		}
	}
	return false
}

// dedupe is paths without repeats, sorted.
func dedupe(paths []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, p := range paths {
		if !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out
}
