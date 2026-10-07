package previewapply

import (
	"go/parser"
	"go/token"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func joined(argv []string) string { return strings.Join(argv, " ") }

func TestTheStandardIsWrittenWhereTheListIs(t *testing.T) {
	hygiene.Isolate(t)
	f, err := parser.ParseFile(token.NewFileSet(), "allowlist.go", nil, parser.ParseComments|parser.PackageClauseOnly)
	if err != nil {
		t.Fatal(err)
	}
	doc := strings.ToLower(f.Doc.Text())
	for _, phrase := range []string{"no user-visible mutation", "ref updates", "index writes", "credential emission", "ruling", "cache-like"} {
		if !strings.Contains(doc, phrase) {
			t.Errorf("the package documentation beside the allowlist does not state %q", phrase)
		}
	}
	for c, clause := range Categories {
		if len(strings.TrimSpace(clause)) <= 20 {
			t.Errorf("category %s needs a clause of the standard, not a label: %q", c, clause)
		}
	}
	if !strings.Contains(strings.ToLower(Categories[ScratchWrite]), "ref") || !strings.Contains(Categories[ScratchWrite], "index") || !strings.Contains(Categories[ScratchWrite], "worktree") {
		t.Errorf("the scratch-write clause must name what it still forbids: %q", Categories[ScratchWrite])
	}
}

func TestEveryEntryDeclaresItselfAgainstTheStandard(t *testing.T) {
	hygiene.Isolate(t)
	seen := map[string]bool{}
	for _, e := range Allowlist {
		name := joined(e.Argv)
		if _, ok := Categories[e.Category]; !ok {
			t.Errorf("%s declares category %q, which is not one of the standard's", name, e.Category)
		}
		if strings.TrimSpace(e.Reason) == "" {
			t.Errorf("%s carries no reason", name)
		}
		if len(e.Argv) < 2 {
			t.Errorf("%q is a single token: it would make every invocation of that program run under --dry-run", e.Argv)
		}
		for _, tok := range e.Argv {
			if tok == "" {
				t.Errorf("%q holds an empty token", e.Argv)
			}
		}
		if seen[name] {
			t.Errorf("%s is listed twice", name)
		}
		seen[name] = true
	}
}

// One entry covering another leaves the narrower one dead, and its reason is
// then not the reason anything is admitted.
func TestNoEntryIsAPrefixOfAnother(t *testing.T) {
	hygiene.Isolate(t)
	for _, a := range Allowlist {
		for _, b := range Allowlist {
			if len(a.Argv) < len(b.Argv) && slices.Equal(b.Argv[:len(a.Argv)], a.Argv) {
				t.Errorf("%q already covers %q", a.Argv, b.Argv)
			}
		}
	}
}

func TestPrefixesIsTheListInStrictclisShape(t *testing.T) {
	hygiene.Isolate(t)
	p := Prefixes()
	if len(p) != len(Allowlist) {
		t.Fatalf("%d prefixes for %d entries", len(p), len(Allowlist))
	}
	for i, e := range Allowlist {
		if !slices.Equal(p[i], e.Argv) {
			t.Errorf("prefix %d is %q, entry is %q", i, p[i], e.Argv)
		}
	}
	p[0][0] = "changed"
	if Allowlist[0].Argv[0] == "changed" {
		t.Fatal("Prefixes shares its slices with the allowlist")
	}
}

// The argvs the standard forbids, none of which any prefix may match.
var forbidden = map[string][][]string{
	"a ref update": {
		{"git", "push", "origin", "HEAD:refs/heads/main"},
		{"git", "push", "--tags"},
		{"git", "fetch", "--prune"},
		{"git", "fetch", "--all"},
		{"git", "fetch", "origin", "--tags"},
		{"git", "fetch", "origin", "--prune"},
		{"git", "fetch", "origin", "--quiet"},
		{"git", "update-ref", "refs/heads/main", "HEAD"},
		{"git", "commit", "-m", "x"},
		{"git", "tag", "-a", "v1.0.0", "-m", "x"},
		{"git", "checkout", "main"},
		{"git", "reset", "--hard", "HEAD"},
		{"git", "merge", "origin/main"},
		{"git", "rebase", "origin/main"},
		{"git", "stash", "push"},
		{"git", "remote", "add", "origin", "git@example:x/y"},
		{"git", "subtree", "split", "--prefix", "pkg"},
	},
	"a worktree write": {
		{"git", "clone", "/some/repo.git", "/home/user/project"},
		{"git", "init"},
		{"git", "clean", "-fdx"},
		{"git", "checkout", "--", "."},
		{"git", "merge-file", "a", "b", "c"},
		{"git", "merge-file", "-p", "a", "b", "c"},
		{"git", "hash-object", "-w", "file.txt"},
	},
	"an index write": {
		{"git", "add", "."},
		{"git", "status", "--porcelain"},
		{"git", "diff", "--name-only"},
		{"git", "diff-index", "--quiet", "HEAD"},
		{"git", "rm", "--cached", "f"},
	},
	"credential emission": {
		{"gh", "auth", "token"},
		{"gh", "auth", "token", "--hostname", "github.com"},
		{"gh", "auth", "status", "--show-token"},
		{"gh", "auth", "status", "-t"},
		{"git", "credential", "fill"},
	},
	"a remote or local mutation": {
		{"gh", "api", "--method", "POST", "repos/x/y/topics"},
		{"gh", "api", "--method", "PUT", "repos/x/y/topics"},
		{"gh", "api", "repos/x/y", "--method", "DELETE"},
		{"gh", "release", "create", "v1.0.0"},
		{"gh", "release", "delete", "v1.0.0"},
		{"gh", "run", "rerun", "1"},
		{"gh", "workflow", "run", "ci.yml"},
		{"gh", "repo", "create", "x/y"},
		{"gh", "pr", "list"},
		{"npm", "publish"},
		{"npm", "install"},
		{"go", "get", "example.com/m"},
		{"go", "list", "-mod=mod", "all"},
		{"uv", "publish"},
		{"uv", "sync"},
		{"ruff", "check", "--fix", "."},
		{"safegit", "commit", "-m", "x"},
		{"saferm", "delete", "f"},
		{"selfdoc", "gen"},
	},
}

func TestTheBansHold(t *testing.T) {
	hygiene.Isolate(t)
	for ban, argvs := range forbidden {
		for _, argv := range argvs {
			if hits := Matching(argv); len(hits) > 0 {
				t.Errorf("%s is %s, and it matches %q, so it would really run under --dry-run", joined(argv), ban, hits[0].Argv)
			}
		}
	}
}

// The other half: the argvs rlsbl issues to read must stay observable.
func TestTheReadsStillMatch(t *testing.T) {
	hygiene.Isolate(t)
	for _, argv := range [][]string{
		{"git", "rev-parse", "HEAD"},
		{"git", "--no-optional-locks", "status", "--porcelain", "-z"},
		{"git", "--no-optional-locks", "diff", "--name-only"},
		{"git", "--no-optional-locks", "diff-index", "--quiet", "HEAD"},
		{"git", "hash-object", "-w", "--stdin"},
		{"git", "merge-file", "--object-id", "-L", "ours", "a", "b", "c"},
		{"git", "cat-file", "--batch"},
		{"git", "tag", "--list", "v*"},
		{"git", "symbolic-ref", "--quiet", "--short", "HEAD"},
		{"gh", "api", "--method", "GET", "repos/x/y"},
		{"gh", "auth", "status", "--hostname", "github.com"},
		{"gh", "run", "view", "1", "--log-failed"},
		{"npm", "view", "widget", "version"},
		{"go", "list", "-m", "all"},
		{"go", "list", "-e", "-f", "{{.Name}}", "./..."},
	} {
		if !Allowed(argv) {
			t.Errorf("%s is a read but matches no entry", joined(argv))
		}
	}
}

// git fetch stands against the ref-update ban by an explicit ruling, which
// covers the pinned argv and nothing shorter.
func TestTheFetchIsRetainedByARulingThatStopsAtThePin(t *testing.T) {
	hygiene.Isolate(t)
	pinned := []string{"git", "fetch", "origin", "--quiet", "--no-tags"}
	hits := Matching(pinned)
	if len(hits) != 1 {
		t.Fatalf("the pinned fetch matches %d entries", len(hits))
	}
	if !strings.Contains(hits[0].Reason, "ruling") {
		t.Errorf("the fetch entry does not say it stands by a ruling: %q", hits[0].Reason)
	}
}

func TestTheScratchWritesAreThePinnedObjectWrites(t *testing.T) {
	hygiene.Isolate(t)
	var scratch []string
	for _, e := range Allowlist {
		if e.Category == ScratchWrite {
			scratch = append(scratch, joined(e.Argv))
		}
	}
	want := []string{"git hash-object -w --stdin", "git merge-file --object-id"}
	if !slices.Equal(scratch, want) {
		t.Fatalf("scratch-write entries = %q, want %q", scratch, want)
	}
}

func TestGitSubcommand(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		argv []string
		want string
	}{
		{[]string{"git", "push", "origin", "main"}, "push"},
		{[]string{"git", "-C", "/repo", "commit", "-m", "x"}, "commit"},
		{[]string{"git", "-c", "user.name=x", "tag", "v1"}, "tag"},
		{[]string{"git", "--no-optional-locks", "status"}, "status"},
		{[]string{"git", "--git-dir", "/x/.git", "log"}, "log"},
		{[]string{"/usr/bin/git", "fetch"}, "fetch"},
		{[]string{"gh", "release", "view"}, ""},
		{[]string{"git"}, ""},
		{nil, ""},
	}
	for _, c := range cases {
		if got := GitSubcommand(c.argv); got != c.want {
			t.Errorf("GitSubcommand(%q) = %q, want %q", c.argv, got, c.want)
		}
	}
}
