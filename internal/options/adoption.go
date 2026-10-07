package options

import (
	"fmt"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// SetCommand is the `rlsbl options set` invocation writing one entry; scope
// is empty for an entry without one.
func SetCommand(name, current, ideal, reason, scope string) string {
	command := fmt.Sprintf("rlsbl options set %s%s --current %s --ideal %s", Prefix, name, current, ideal)
	if scope != "" {
		command += " --scope " + scope
	}
	return command + fmt.Sprintf(" --reason %q", reason)
}

// MemberScope is the scope an entry for the member at memberPath alone
// carries: the member's path in a workspace, and none in a standalone
// repository, whose one project an entry without a scope covers.
func MemberScope(d *declarations.Releasables, memberPath string) string {
	if d == nil || !d.IsWorkspace() {
		return ""
	}
	return memberPath
}

// SettingsProblems are every adoption declaration that disagrees with its
// option: declared while the option is off (nothing reads it), or missing
// while the option is on. A member's internal_dep_floors follows
// rlsbl:dep-floors for that member; the test runner's settings,
// .strictmetadata/test-runner/test-runner.toml, follow rlsbl:test-sandbox
// for the root member, since the runner serves the whole repository.
// testRunnerFound is whether that file exists.
func (o *Options) SettingsProblems(d *declarations.Releasables, testRunnerFound bool) ([]string, error) {
	var problems []string
	for _, m := range d.Members {
		v, err := o.Value(DepFloors, m.Path)
		if err != nil {
			return nil, err
		}
		declared := len(m.InternalDepFloors) > 0
		where := fmt.Sprintf("the member %q (path %q) in %s", m.Name, m.Path, declarations.ReleasablesFile)
		switch {
		case v.Value == Off && declared:
			strongest := o.reg.Strongest(DepFloors)
			problems = append(problems, fmt.Sprintf(
				"%s declares internal_dep_floors, but %s%s is off for it (%s), so nothing reads them. Delete internal_dep_floors from the member, or switch the option on: %s",
				where, Prefix, DepFloors, v.Source,
				SetCommand(DepFloors, strongest, strongest, "<why this member adopts dep-floors>", MemberScope(d, m.Path))))
		case v.Value != Off && !declared:
			problems = append(problems, fmt.Sprintf(
				"%s%s is %s for %s (%s), but the member declares no internal_dep_floors. Declare internal_dep_floors (the ecosystem-internal package names whose floors are policed), or switch the option off by deleting %s.",
				Prefix, DepFloors, v.Value, where, v.Source, entryLocation(v)))
		}
	}
	v, err := o.Value(TestSandbox, declarations.RootPath)
	if err != nil {
		return nil, err
	}
	switch {
	case v.Value == Off && testRunnerFound:
		strongest := o.reg.Strongest(TestSandbox)
		problems = append(problems, fmt.Sprintf(
			"%s exists, but %s%s is off (%s), so nothing reads it. Delete %s, or switch the option on: %s",
			declarations.TestRunnerFile, Prefix, TestSandbox, v.Source, declarations.TestRunnerDir,
			SetCommand(TestSandbox, strongest, strongest, "<why this repository distributes the sandboxed test runner>", MemberScope(d, declarations.RootPath))))
	case v.Value != Off && !testRunnerFound:
		problems = append(problems, fmt.Sprintf(
			"%s%s is %s (%s), but there is no %s. Write it (the sandboxed test runner's settings), or switch the option off by deleting %s.",
			Prefix, TestSandbox, v.Value, v.Source, declarations.TestRunnerFile, entryLocation(v)))
	}
	return problems, nil
}

// entryLocation names the entry that switched an option on, for the fix
// that deletes it.
func entryLocation(v Value) string {
	if v.Entry == nil {
		return "the entry that switches it on"
	}
	return fmt.Sprintf("entry[%d] (%s) from %s/%s", v.Entry.Index, v.Entry.ID, Dir, v.Entry.File)
}
