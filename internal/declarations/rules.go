package declarations

import (
	"fmt"
	"sort"
	"strings"
	"unicode"
)

// NameProblem says why name cannot name a releasable or a member, and is
// empty when it can. A name is a directory under .strictmetadata/ (a
// changelog, a release directory, a retired history), so it is one path
// segment, and not a hidden one.
func NameProblem(name string) string {
	switch {
	case name == "":
		return "the name is empty"
	case name == "." || name == "..":
		return fmt.Sprintf("the name %q names a directory relative to itself", name)
	case strings.HasPrefix(name, "."):
		return fmt.Sprintf("the name %q starts with a dot, which would make its directories under %s hidden, the mark of generated ones", name, MetadataDir)
	case strings.ContainsAny(name, "/\\"):
		return fmt.Sprintf("the name %q holds a path separator; a name is one directory under %s", name, MetadataDir)
	}
	for _, r := range name {
		if unicode.IsSpace(r) || unicode.IsControl(r) {
			return fmt.Sprintf("the name %q holds whitespace or a control character", name)
		}
	}
	return ""
}

// TagFormatProblem says why format cannot be the tag format of the
// releasable named name, and is empty when it can. A tag format holds one
// {version} placeholder, optionally {name}, and no other brace; rendered, it
// is a tag name git accepts.
func TagFormatProblem(format, name string) string {
	if n := strings.Count(format, "{version}"); n != 1 {
		return fmt.Sprintf("the tag format %q holds %d {version} placeholders; a tag format holds one", format, n)
	}
	rest := strings.ReplaceAll(strings.ReplaceAll(format, "{version}", ""), "{name}", "")
	if strings.ContainsAny(rest, "{}") {
		return fmt.Sprintf("the tag format %q holds a brace outside {version} and {name}, the only placeholders a tag format has", format)
	}
	sample := strings.ReplaceAll(strings.ReplaceAll(format, "{name}", name), "{version}", "0.1.0")
	if problem := RefNameProblem(sample); problem != "" {
		return fmt.Sprintf("the tag format %q renders tags like %q, which git refuses: %s", format, sample, problem)
	}
	return ""
}

// RefNameProblem says why git refuses name as a tag name (git
// check-ref-format's rules), and is empty when git accepts it.
func RefNameProblem(name string) string {
	switch {
	case name == "":
		return "it is empty"
	case name == "@":
		return "it is \"@\""
	case strings.HasPrefix(name, "/") || strings.HasSuffix(name, "/"):
		return "it starts or ends with '/'"
	case strings.HasPrefix(name, "-"):
		return "it starts with '-'"
	case strings.HasSuffix(name, "."):
		return "it ends with '.'"
	case strings.Contains(name, "//"):
		return "it holds '//'"
	case strings.Contains(name, ".."):
		return "it holds '..'"
	case strings.Contains(name, "@{"):
		return "it holds '@{'"
	}
	for _, r := range name {
		if r < 0x20 || r == 0x7f || strings.ContainsRune(" ~^:?*[\\", r) {
			return fmt.Sprintf("it holds %q", r)
		}
	}
	for _, component := range strings.Split(name, "/") {
		if strings.HasPrefix(component, ".") {
			return fmt.Sprintf("its component %q starts with '.'", component)
		}
		if strings.HasSuffix(component, ".lock") {
			return fmt.Sprintf("its component %q ends with \".lock\"", component)
		}
	}
	return ""
}

// MemberForPath is the member whose territory the canonical repository-
// relative path p lies in: the member with the most specific path claiming
// it, the root member claiming whatever no other member does. ok is false
// only for declarations without a root member, where an unclaimed path has
// no member.
func (d *Releasables) MemberForPath(p string) (m Member, ok bool) {
	best := -1
	for _, candidate := range d.Members {
		length := 0
		if !candidate.IsRoot() {
			if !IsInside(p, candidate.Path) {
				continue
			}
			length = len(candidate.Path)
		}
		if length > best {
			m, ok, best = candidate, true, length
		}
	}
	return m, ok
}

// envAssignment reports whether word is a VAR=value assignment.
func envAssignment(word string) bool {
	name, _, found := strings.Cut(word, "=")
	if !found || name == "" {
		return false
	}
	for i, r := range name {
		letter := r == '_' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z'
		digit := r >= '0' && r <= '9'
		if !letter && !(digit && i > 0) {
			return false
		}
	}
	return true
}

// duplicates are the values of list that occur more than once, sorted.
func duplicates(list []string) []string {
	seen := map[string]int{}
	for _, v := range list {
		seen[v]++
	}
	var out []string
	for v, n := range seen {
		if n > 1 {
			out = append(out, v)
		}
	}
	sort.Strings(out)
	return out
}

// check runs the declaration rules: everything that relates one part of the
// document to another, or that a schema cannot state. Every problem found is
// returned, in document order.
func check(d *Releasables) []string {
	var problems []string
	add := func(format string, args ...any) { problems = append(problems, fmt.Sprintf(format, args...)) }

	switch d.Layout {
	case LayoutStandalone, LayoutWorkspace:
	default:
		add("repository_layout is %q; declare \"standalone\" or \"workspace\"", d.Layout)
	}
	if len(d.ReleaseBranches) == 0 {
		add("release_branches is empty; name the branches a release may run from")
	}
	for _, b := range duplicates(d.ReleaseBranches) {
		add("release_branches names %q more than once", b)
	}
	for _, b := range d.ReleaseBranches {
		if problem := RefNameProblem(b); problem != "" {
			add("release_branches names %q, which git refuses as a branch name: %s", b, problem)
		}
	}

	problems = append(problems, checkMembers(d)...)
	problems = append(problems, checkReleasables(d)...)
	problems = append(problems, checkLayout(d)...)
	return problems
}

// checkLayout checks what each layout requires of the member list.
func checkLayout(d *Releasables) []string {
	var problems []string
	if d.Layout != LayoutStandalone {
		return nil
	}
	if len(d.Members) != 1 || !d.Members[0].IsRoot() {
		var paths []string
		for _, m := range d.Members {
			paths = append(paths, m.Path)
		}
		problems = append(problems, fmt.Sprintf("repository_layout is \"standalone\", which declares one member, the root (path = \".\"), and this file declares members at %s. A repository of several members declares repository_layout = \"workspace\".", strings.Join(paths, ", ")))
	}
	if len(d.Releasables) != 1 {
		problems = append(problems, "repository_layout is \"standalone\", which declares one releasable, the one its root member is versioned under; declare one [[releasables]] table")
	}
	root := d.RootMember()
	if len(d.Members) == 1 && root.IsRoot() && !root.Versioned() {
		problems = append(problems, "repository_layout is \"standalone\" and its root member is versioned under no releasable; a standalone project releases its root, so set its releasable to the declared releasable's name")
	}
	return problems
}

// rootMemberSnippet is the root member a refusal shows, in the form that
// loads as it is written.
const rootMemberSnippet = "  [[members]]\n  path = \".\"\n  name = \"root\"\n  dev_only = true\n  releasable = false\n"

func checkMembers(d *Releasables) []string {
	var problems []string
	add := func(format string, args ...any) { problems = append(problems, fmt.Sprintf(format, args...)) }

	names := map[string]bool{}
	var roots []string
	pathOwner := map[string]string{}
	for i, m := range d.Members {
		where := memberLabel(i, m.Name)
		if problem := PathProblem(m.Path); problem != "" {
			add("%s: %s", where, problem)
		}
		if problem := NameProblem(m.Name); problem != "" {
			add("%s: %s", where, problem)
		}
		if names[m.Name] {
			add("%s: another member is named %q too; a name names one member", where, m.Name)
		}
		names[m.Name] = true
		if other, ok := pathOwner[m.Path]; ok {
			add("%s: the member %q declares the path %q too. A file has one owner, so a path has one member: merge the two entries, or give one a path of its own.", where, other, m.Path)
		} else {
			pathOwner[m.Path] = m.Name
		}
		if m.IsRoot() {
			roots = append(roots, m.Name)
			if m.Name != RootName {
				add("%s: the root member (path = \".\") is named %q, and %q is the only name it may have: job keys, router filters, and check patterns derive from it. Set name = %q.", where, m.Name, RootName, RootName)
			}
		} else if m.Name == RootName {
			add("%s: the member at %q is named %q, which is reserved for the member at path = \".\". Rename it; rlsbl will not guess which member owns the repository root.", where, m.Path, RootName)
		}
	}
	switch {
	case len(roots) == 0 && len(d.Members) > 0:
		add("no member declares the repository root (path = \".\"). Every repository declares its root as a member: it owns every file no other member claims, so no file is outside the ownership model. Add one, versioned under a releasable (releasable = \"<name>\") when its files need changelog coverage, or as a dev node:\n\n%s", rootMemberSnippet)
	case len(roots) > 1:
		add("the members %s all declare the repository root (path = \".\"); one member owns it. Keep one and give the others paths of their own.", strings.Join(roots, ", "))
	}

	checks := map[string]string{}
	for i, m := range d.Members {
		where := memberLabel(i, m.Name)
		problems = append(problems, checkMember(d, m, where, checks)...)
	}
	return problems
}

func checkMember(d *Releasables, m Member, where string, checkOwners map[string]string) []string {
	var problems []string
	add := func(format string, args ...any) {
		problems = append(problems, where+": "+fmt.Sprintf(format, args...))
	}

	if m.Releasable != "" {
		if _, ok := d.Releasable(m.Releasable); !ok {
			var declared []string
			for _, r := range d.Releasables {
				declared = append(declared, r.Name)
			}
			add("releasable = %q names no declared releasable (declared: %s); name one of them, or write false", m.Releasable, strings.Join(declared, ", "))
		}
	}

	for _, dep := range duplicates(m.DependsOn) {
		add("depends_on names %q more than once", dep)
	}
	for _, dep := range m.DependsOn {
		switch _, ok := d.Member(dep); {
		case dep == m.Name:
			add("depends_on names the member itself")
		case !ok:
			add("depends_on names %q, which no member is named", dep)
		}
	}
	for _, floor := range duplicates(m.InternalDepFloors) {
		add("internal_dep_floors names %q more than once", floor)
	}
	for _, allowed := range duplicates(m.LintAllow) {
		add("lint_allow names %q more than once", allowed)
	}

	// A path the member declares must stay in its own territory: a directory
	// a nested member owns is that member's.
	ownTerritory := func(label, rel string) {
		if problem := PathProblem(rel); problem != "" {
			add("%s: %s", label, problem)
			return
		}
		full := Join(m.Path, rel)
		if owner, ok := d.MemberForPath(full); ok && owner.Name != m.Name {
			add("%s %q lies in the member %q (%s), which owns that directory; declare it on that member instead", label, rel, owner.Name, owner.Path)
		}
	}

	targets := map[string]bool{}
	for _, t := range m.Targets {
		targets[t.Name] = true
		if t.Path != "" {
			ownTerritory(fmt.Sprintf("the %s target's path", t.Name), t.Path)
		}
	}
	checkHooks(m.Hooks, func(label, dir string) { ownTerritory(label, dir) }, add)

	for _, c := range m.ExternalChecks {
		label := fmt.Sprintf("the external check %q", c.Name)
		if owner, ok := checkOwners[c.Name]; ok {
			add("%s is declared by the member %q too; a check name names one check", label, owner)
		} else {
			checkOwners[c.Name] = m.Name
		}
		fields := strings.Fields(c.Command)
		if len(fields) == 0 {
			add("%s has an empty command", label)
		} else if envAssignment(fields[0]) {
			add("%s starts its command with the assignment %q; write it as env %s ..., so the command's first word is the program it runs", label, fields[0], fields[0])
		}
		for _, dep := range duplicates(c.DependsOn) {
			add("%s names %q in depends_on more than once", label, dep)
		}
		for _, dep := range c.DependsOn {
			if dep == c.Name {
				add("%s depends on itself", label)
			}
		}
		if c.Cwd != "" {
			ownTerritory(label+"'s cwd", c.Cwd)
		}
	}

	pipelines := map[string]Pipeline{}
	for _, p := range m.Pipelines {
		pipelines[p.Name] = p
	}
	for _, p := range m.Pipelines {
		label := fmt.Sprintf("the pipeline %q", p.Name)
		switch {
		case len(m.Targets) == 0:
			add("%s publishes the %s target, and the member declares no targets; a pipeline publishes a target its member declares, so declare targets = [{ name = %q }]", label, p.Target, p.Target)
		case !targets[p.Target]:
			add("%s publishes the %s target, which the member does not declare", label, p.Target)
		}
		if p.Type != p.Target {
			add("%s is of type %s and publishes the %s target; a pipeline's type is the ecosystem of the target it publishes", label, p.Type, p.Target)
		}
		switch p.Type {
		case TargetGo:
			if p.Artifact != ArtifactBinary && p.Artifact != ArtifactLibrary {
				add("%s is a go pipeline, whose artifact is %q or %q, not %q", label, ArtifactBinary, ArtifactLibrary, p.Artifact)
			}
			if p.Local && len(p.InstallPaths) == 0 {
				add("%s publishes locally and declares no install_paths, the main packages it installs", label)
			}
			if p.HomebrewTap != "" && p.Artifact != ArtifactBinary {
				add("%s declares homebrew_tap, which only a go binary pipeline publishes to", label)
			}
		default:
			if p.Artifact != ArtifactPackage && p.Artifact != ArtifactGoBinary {
				add("%s is a %s pipeline, whose artifact is %q or %q, not %q", label, p.Type, ArtifactPackage, ArtifactGoBinary, p.Artifact)
			}
			if len(p.InstallPaths) > 0 {
				add("%s declares install_paths, which only a go pipeline installs", label)
			}
			if p.HomebrewTap != "" {
				add("%s declares homebrew_tap, which only a go binary pipeline publishes to", label)
			}
		}
		for _, ip := range duplicates(p.InstallPaths) {
			add("%s names the install path %q more than once", label, ip)
		}
		if p.Artifact == ArtifactGoBinary {
			wrapped, ok := pipelines[p.BinaryPipeline]
			switch {
			case p.BinaryPipeline == "":
				add("%s publishes go-binary packages and names no binary_pipeline, the go binary pipeline whose binaries it carries", label)
			case !ok:
				add("%s names binary_pipeline %q, which the member does not declare", label, p.BinaryPipeline)
			case wrapped.Type != TargetGo || wrapped.Artifact != ArtifactBinary:
				add("%s names binary_pipeline %q, which is not a go pipeline whose artifact is binary", label, p.BinaryPipeline)
			}
		} else if p.BinaryPipeline != "" {
			add("%s declares binary_pipeline, which only a go-binary pipeline wraps", label)
		}
	}
	return problems
}

// checkHooks runs dir on every hook that declares a directory, and refuses
// an empty command line.
func checkHooks(h Hooks, dir func(label, dir string), add func(string, ...any)) {
	points := [][]Hook{h.PreChecks, h.PreRelease, h.PostRelease}
	for i, hooks := range points {
		for j, hook := range hooks {
			label := fmt.Sprintf("hooks.%s[%d]", hookPoints[i], j)
			if strings.TrimSpace(hook.Command) == "" {
				add("%s has an empty command line; delete it", label)
			}
			if hook.Dir != "" {
				dir(label+"'s dir", hook.Dir)
			}
		}
	}
}

func checkReleasables(d *Releasables) []string {
	var problems []string
	root := d.RootMember()
	patterns := map[string]string{}
	for i, r := range d.Releasables {
		pattern := strings.ReplaceAll(r.TagFormat, "{name}", r.Name)
		if other, ok := patterns[pattern]; ok {
			problems = append(problems, fmt.Sprintf("%s: its tag format renders the tags %q, as the releasable %q's does; a tag names one releasable, so give one of them another tag format", releasableLabel(i, r.Name), pattern, other))
		} else {
			patterns[pattern] = r.Name
		}
	}
	for i, r := range d.Releasables {
		where := releasableLabel(i, r.Name)
		add := func(format string, args ...any) {
			problems = append(problems, where+": "+fmt.Sprintf(format, args...))
		}
		if problem := NameProblem(r.Name); problem != "" {
			add("%s", problem)
		}
		if problem := TagFormatProblem(r.TagFormat, r.Name); problem != "" {
			add("%s", problem)
		}
		switch r.PublishMode {
		case PublishCI, PublishNone:
		default:
			add("publish_mode is %q; declare \"ci\" or \"none\"", r.PublishMode)
		}
		members := d.MembersOf(r.Name)
		if len(members) == 0 {
			add("no member is versioned under it, so it releases nothing; name it in a member's releasable, or delete the table")
		}

		needsPattern := d.Layout == LayoutWorkspace && root.Releasable == r.Name && r.PublishMode == PublishCI
		switch {
		case needsPattern && r.PublishCICheckPattern == "":
			add("it owns the root member of a workspace and publishes from CI, so it declares publish_ci_check_pattern: the pattern of the check-run names the root's own CI reports, which publishing waits for on the release commit. rlsbl cannot infer those names. Add, for example:\n\n  publish_ci_check_pattern = '^(test|lint)( \\(.*\\))?$'\n")
		case !needsPattern && r.PublishCICheckPattern != "":
			add("publish_ci_check_pattern is read only for the releasable owning the root member of a workspace that publishes from CI, and nothing reads it here; delete the line")
		}

		for j, word := range r.DeployCommand {
			if strings.TrimSpace(word) == "" {
				add("deploy_command[%d] is empty", j)
			}
		}

		// A releasable's hook runs from the repository root, or from its dir,
		// which must lie in one of the releasable's members.
		checkHooks(r.Hooks, func(label, dir string) {
			if problem := PathProblem(dir); problem != "" {
				add("%s: %s", label, problem)
				return
			}
			owner, ok := d.MemberForPath(dir)
			if ok && owner.Releasable != r.Name {
				add("%s %q lies in the member %q, which is not versioned under this releasable; a releasable's hook runs in one of its own members", label, dir, owner.Name)
			}
		}, add)
	}
	return problems
}
