// Package previewapply is the observe, preview, apply skeleton of rlsbl's
// reconcilers, and the observe allowlist that decides which programs may run
// while rlsbl is only looking.
//
// # The observe allowlist and its standard
//
// An observe is a program that really runs under --dry-run instead of being
// recorded. strictcli matches an argv against the allowlist's prefixes
// element by element, by string equality; a match means the run is performed
// for real and is legal even inside a read_only command. The same list
// screens every program a reconciler's observation starts. That makes this
// list the one place where a preview may touch the outside world, so every
// entry carries a category and a reason.
//
// The standard is no user-visible mutation: an allowlisted program may not
// change anything a user would notice.
//
// Admitted under the standard, each as a category:
//
//   - reads of local state: the working tree, the object database, refs,
//     config, files;
//   - reads of a remote over the network, which change nothing on the far
//     side (a tool's own download cache may fill, which is invisible
//     plumbing: it changes no project state, no output, and no later
//     decision);
//   - a tool reporting its own version or authentication state;
//   - scratch writes: loose objects added to an object database. A program
//     qualifies only if it writes no ref, no index, and no worktree state:
//     a loose object is unreachable garbage until a ref names it, and
//     deleting it costs a recomputation and nothing else.
//
// Refused by the standard:
//
//   - ref updates: writing any ref, remote-tracking refs and FETCH_HEAD
//     included;
//   - index writes: anything that takes index.lock, which in a worktree
//     several sessions share can make a concurrent commit fail;
//   - credential emission: printing a live token to stdout, where a preview
//     would put a secret on a pipe, into a buffer, or into a log.
//
// One entry stands against the ref-update ban by an explicit ruling: the
// pinned `git fetch origin --quiet --no-tags`. FETCH_HEAD and origin's remote-tracking
// refs are cache-like git plumbing no user workflow reads as state (deleting
// them costs a re-fetch), unlike refs/heads or the index. The pin keeps the
// ruling narrow: the short mutating forms (git fetch --prune, --tags, --all)
// cannot match it. Prefix matching cannot refuse a flag appended after a
// pinned argv; rlsbl's own call sites are the only producer of these argvs,
// so what a pin buys is that no call site reaches a mutating form by writing
// a shorter argv.
//
// git status, diff, and diff-index appear only with --no-optional-locks,
// here and at every call site: without it they refresh the index and take
// index.lock. go list appears only as the two whole argvs rlsbl issues
// (GoBuildList and GoPackageListing, which the call sites use), because the
// bare prefix, or any prefix ending before the package or module pattern,
// also admits go list -mod=mod, which rewrites go.mod and go.sum; go reads
// no flag after the first pattern, so nothing appended to them is a flag.
// gh auth status is pinned to --hostname github.com, because the
// bare prefix also admits --show-token and -t, which print the credential.
// No entry is a single token: that would make every invocation of the
// program an observe.
package previewapply

import (
	"slices"
	"strings"
)

// Category is the clause of the standard an allowlist entry is admitted
// under.
type Category string

// The closed set of categories.
const (
	LocalRead    Category = "local-read"
	NetworkRead  Category = "network-read"
	SelfReport   Category = "self-report"
	ScratchWrite Category = "scratch-write"
)

// Categories maps each category to the clause of the standard that admits
// it. An entry declaring a category outside this map fails the tests, so the
// standard cannot be widened by adding an entry.
var Categories = map[Category]string{
	LocalRead:    "Reads local state (working tree, objects, refs, config, files) and writes nothing.",
	NetworkRead:  "Reads a remote over the network. Nothing on the far side changes; a local tool cache may be populated, which the standard admits as invisible plumbing.",
	SelfReport:   "The tool reports its own version or authentication state. No project state and no remote state is read or written.",
	ScratchWrite: "Writes only scratch: loose objects in an object database. No ref, no index, and no worktree state of any repository is touched, so nothing a user would notice changes.",
}

// Entry is one allowlist entry: the argv prefix, its category, and why it
// is admitted.
type Entry struct {
	Argv     []string
	Category Category
	Reason   string
}

// The go list argvs rlsbl issues while observing, each pinned whole in the
// allowlist and used as is by its call site.
var (
	// GoBuildList prints the build list of the module or workspace in the
	// working directory as a stream of JSON objects.
	GoBuildList = []string{"go", "list", "-m", "-json", "all"}
	// GoPackageListing prints one line per package of the module in the
	// working directory: its name, import path, and directory, tab-separated.
	GoPackageListing = []string{"go", "list", "-e", "-f", "{{.Name}}\t{{.ImportPath}}\t{{.Dir}}", "./..."}
)

// Allowlist is the observe allowlist.
var Allowlist = []Entry{
	{[]string{"git", "rev-parse"}, LocalRead, "resolves refs and paths"},
	{[]string{"git", "rev-list"}, LocalRead, "walks the commit graph"},
	{[]string{"git", "--no-optional-locks", "status"}, LocalRead, "reports worktree state; the flag keeps it off index.lock"},
	{[]string{"git", "log"}, LocalRead, "reads commit history"},
	{[]string{"git", "show"}, LocalRead, "reads an object"},
	{[]string{"git", "describe"}, LocalRead, "names a commit from tags"},
	{[]string{"git", "fetch", "origin", "--quiet", "--no-tags"}, NetworkRead, "the one fetch the release flow needs; retained by explicit ruling against the ref-update ban because FETCH_HEAD and origin's remote-tracking refs are cache-like git plumbing no user workflow reads as state, unlike refs/heads or the index; --no-tags keeps it from creating local tags (a plain fetch follows the tags on the commits it brings), and the argv is pinned so the short mutating forms (--prune, --tags, --all, or the fetch without --no-tags) cannot match"},
	{[]string{"git", "--no-optional-locks", "diff"}, LocalRead, "compares trees; the flag keeps it off index.lock"},
	{[]string{"git", "diff-tree"}, LocalRead, "compares two trees, no index"},
	{[]string{"git", "--no-optional-locks", "diff-index"}, LocalRead, "compares a tree against the index; the flag keeps it from refreshing"},
	{[]string{"git", "ls-files"}, LocalRead, "lists tracked and ignored paths"},
	{[]string{"git", "ls-remote"}, NetworkRead, "lists a remote's refs"},
	{[]string{"git", "ls-tree"}, LocalRead, "lists a tree's entries"},
	{[]string{"git", "cat-file"}, LocalRead, "reads an object"},
	{[]string{"git", "merge-base"}, LocalRead, "computes a merge base and answers ancestry"},
	{[]string{"git", "check-ignore"}, LocalRead, "tests paths against ignore rules"},
	{[]string{"git", "for-each-ref"}, LocalRead, "lists refs"},
	{[]string{"git", "symbolic-ref"}, LocalRead, "reads HEAD's target"},
	{[]string{"git", "name-rev"}, LocalRead, "names a commit from refs"},
	{[]string{"git", "shortlog"}, LocalRead, "summarizes history"},
	{[]string{"git", "var"}, LocalRead, "reads a git variable"},
	{[]string{"git", "config", "--get"}, LocalRead, "reads one config key"},
	{[]string{"git", "config", "--get-all"}, LocalRead, "reads config values"},
	{[]string{"git", "config", "--list"}, LocalRead, "lists config"},
	{[]string{"git", "remote", "get-url"}, LocalRead, "reads a remote URL"},
	{[]string{"git", "branch", "--show-current"}, LocalRead, "reads the branch"},
	{[]string{"git", "branch", "--contains"}, LocalRead, "lists containing branches"},
	{[]string{"git", "branch", "-a"}, LocalRead, "lists branches"},
	{[]string{"git", "branch", "--list"}, LocalRead, "lists branches"},
	{[]string{"git", "tag", "-l"}, LocalRead, "lists tags"},
	{[]string{"git", "tag", "--list"}, LocalRead, "lists tags"},
	{[]string{"git", "tag", "--points-at"}, LocalRead, "lists tags at a commit"},
	{[]string{"git", "stash", "list"}, LocalRead, "lists stash entries"},
	{[]string{"git", "--version"}, SelfReport, "prints git's version"},
	{[]string{"git", "hash-object", "--"}, LocalRead, "hashes working-tree files as staging them would store them, writing nothing; pinned with -- so no flag (-w) can follow"},
	{[]string{"git", "hash-object", "-w", "--stdin"}, ScratchWrite, "stores the three sides of a three-way merge as loose blobs, read from stdin; it writes no ref, takes no index lock, and leaves the worktree alone, and an unreferenced blob is scratch until garbage collection"},
	{[]string{"git", "merge-file", "--object-id"}, ScratchWrite, "three-way merges blobs and stores the result as one more loose blob, printing its id; --object-id is what makes it read and write objects instead of overwriting the first file operand"},
	{[]string{"gh", "api", "--method", "GET"}, NetworkRead, "GET-pinned API read; the bare `gh api` prefix would admit POST"},
	{[]string{"gh", "auth", "status", "--hostname", "github.com"}, SelfReport, "reports whether a credential for github.com is present and valid; pinned to the one argv rlsbl issues because the bare `gh auth status` prefix also admits --show-token and -t, which print the live credential to stdout"},
	{[]string{"gh", "release", "view"}, NetworkRead, "reads one Release"},
	{[]string{"gh", "release", "list"}, NetworkRead, "lists Releases"},
	{[]string{"gh", "repo", "view"}, NetworkRead, "reads repository metadata"},
	{[]string{"gh", "run", "list"}, NetworkRead, "lists workflow runs"},
	{[]string{"gh", "run", "view"}, NetworkRead, "reads one workflow run"},
	{[]string{"gh", "run", "watch"}, NetworkRead, "blocks polling one workflow run until it concludes; nothing on the far side changes, and a preview that recorded it would report a pass it never observed"},
	{[]string{"gh", "workflow", "list"}, NetworkRead, "lists workflows"},
	{[]string{"gh", "--version"}, SelfReport, "prints gh's version"},
	{[]string{"npm", "view"}, NetworkRead, "registry metadata read; its only write is npm's own cache"},
	{[]string{"npm", "token", "list", "--json"}, NetworkRead, "lists the npm account's tokens, each shown truncated, with its creation time (the npm-token-synced check); pinned to the one argv rlsbl issues because the bare `npm token` prefix also admits create and revoke"},
	{GoBuildList, NetworkRead, "reads the build list of a Go module or workspace (the release's go work sync check); its only write is the module cache"},
	{GoPackageListing, LocalRead, "package enumeration; -e keeps it off the network and the format string only shapes stdout"},
	{[]string{"go", "mod", "tidy", "-diff"}, NetworkRead, "reports what `go mod tidy` would change without changing go.mod or go.sum (the release's untidy-module check); its only write is the module cache"},
	{[]string{"go", "mod", "edit", "-json"}, LocalRead, "prints a module's go.mod as JSON; -json prints the result instead of writing go.mod"},
	{[]string{"go", "work", "edit", "-json"}, LocalRead, "prints go.work as JSON; -json prints the result instead of writing go.work"},
	{[]string{"uv", "--version"}, SelfReport, "prints uv's version"},
	{[]string{"safegit", "--version"}, SelfReport, "prints safegit's version"},
	{[]string{"saferm", "--version"}, SelfReport, "prints saferm's version"},
	{[]string{"selfdoc", "--version"}, SelfReport, "prints selfdoc's version"},
}

// Prefixes is the allowlist in the shape strictcli.WithProcObserveAllowlist
// takes it.
func Prefixes() [][]string {
	out := make([][]string, len(Allowlist))
	for i, e := range Allowlist {
		out[i] = slices.Clone(e.Argv)
	}
	return out
}

// Allowed reports whether argv matches an allowlist prefix, element by
// element, exactly as strictcli matches its observe allowlist: "may run
// during observation" and "really runs under --dry-run" are the same set.
func Allowed(argv []string) bool {
	return len(Matching(argv)) > 0
}

// Matching lists the entries whose prefix argv matches.
func Matching(argv []string) []Entry {
	var hits []Entry
	for _, e := range Allowlist {
		if len(argv) >= len(e.Argv) && slices.Equal(argv[:len(e.Argv)], e.Argv) {
			hits = append(hits, e)
		}
	}
	return hits
}

// gitOptionsWithValue are git's global options that take a separate value
// token, so the scan does not mistake the value for the subcommand.
var gitOptionsWithValue = map[string]bool{
	"-C": true, "-c": true, "--git-dir": true, "--work-tree": true, "--namespace": true, "--exec-path": true,
}

// GitSubcommand is the git subcommand argv runs, or "" when argv is not a
// git call.
func GitSubcommand(argv []string) string {
	if len(argv) == 0 {
		return ""
	}
	head := argv[0][strings.LastIndex(argv[0], "/")+1:]
	if head != "git" && !strings.HasPrefix(head, "git.") {
		return ""
	}
	for i := 1; i < len(argv); i++ {
		token := argv[i]
		if gitOptionsWithValue[token] {
			i++
			continue
		}
		if strings.HasPrefix(token, "-") {
			continue
		}
		return token
	}
	return ""
}
