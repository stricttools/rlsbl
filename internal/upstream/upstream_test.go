package upstream_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/upstream"
)

const (
	upstreamURL = "https://github.com/up/proj"
	kept        = "refs/tags-of/github.com/up/proj/"
	branchRef   = "refs/upstream/github.com/up/proj/main"
	declaration = "format_version = 1\nhost = \"github.com\"\nowner = \"up\"\nrepo = \"proj\"\nbranch = \"main\"\n"
)

// fork is an upstream with three tags (v0.2.0 annotated), a bare origin
// cloned from it, and the fork's work tree: a clone of origin carrying one
// commit and one tag of its own (nightly), both pushed, declaring the
// upstream. The declared URL is redirected to the local upstream with git's
// url.<base>.insteadOf, so the code runs git ls-remote itself against the
// URL it derives.
type fork struct {
	t        *testing.T
	upstream *testsupport.Repo
	origin   string
	dir      string
}

func newFork(t *testing.T) *fork {
	t.Helper()
	up := testsupport.NewRepo(t)
	up.CommitFile("a.txt", "a\n", "upstream: a")
	up.Git("tag", "v0.1.0")
	up.CommitFile("b.txt", "b\n", "upstream: b")
	up.Git("tag", "-a", "v0.2.0", "-m", "upstream release 0.2.0")
	up.CommitFile("c.txt", "c\n", "upstream: c")
	up.Git("tag", "v0.3.0")

	base := t.TempDir()
	origin := filepath.Join(base, "origin.git")
	if _, stderr, code := testsupport.RunGit(t, base, "clone", "-q", "--bare", "file://"+up.Dir, origin); code != 0 {
		t.Fatalf("clone --bare: %s", stderr)
	}
	workDir := filepath.Join(base, "fork")
	if _, stderr, code := testsupport.RunGit(t, base, "clone", "-q", "file://"+origin, workDir); code != 0 {
		t.Fatalf("clone: %s", stderr)
	}
	f := &fork{t: t, upstream: up, origin: origin, dir: workDir}
	f.git("config", "url.file://"+up.Dir+".insteadOf", upstreamURL)
	testsupport.WriteFile(t, filepath.Join(workDir, "ours.txt"), "ours\n")
	f.git("add", "ours.txt")
	f.git("commit", "-q", "-m", "fork: our own change")
	f.git("tag", "nightly")
	f.git("push", "-q", "origin", "HEAD:refs/heads/main", "refs/tags/nightly")
	f.declare()
	return f
}

// git runs git in the fork's work tree, failing the test on a non-zero
// exit; testsupport.Repo's own methods need the test it was made with.
func (f *fork) git(args ...string) string {
	f.t.Helper()
	stdout, stderr, code := testsupport.RunGit(f.t, f.dir, args...)
	if code != 0 {
		f.t.Fatalf("git %s: %s", strings.Join(args, " "), stderr)
	}
	return strings.TrimSpace(stdout)
}

func (f *fork) declare() {
	testsupport.WriteFile(f.t, filepath.Join(f.dir, ".strictmetadata", "upstream", "manifest.toml"), "owner = \"strictspec\"\n")
	testsupport.WriteFile(f.t, filepath.Join(f.dir, ".strictmetadata", "upstream", "upstream.toml"), declaration)
}

// refs are the refs under prefix of the repository at dir.
func refs(t *testing.T, dir, prefix string) map[string]string {
	t.Helper()
	out := map[string]string{}
	for name, object := range testsupport.Refs(t, dir) {
		if strings.HasPrefix(name, prefix) {
			out[name] = object
		}
	}
	return out
}

func names(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for n := range m {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// adopt runs the adoption in dir as the handler of a mutating command.
func adopt(t *testing.T, dir string, dryRun bool) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		return upstream.RunAdoptTags(ctx, dir)
	})
}

func equalRefs(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

func TestARepositoryWithoutTheFileIsNotAFork(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	if _, found, err := upstream.Load(repo.Dir); err != nil || found {
		t.Fatalf("found %v, err %v", found, err)
	}
	if url, err := upstream.URLOf(repo.Dir); err != nil || url != "" {
		t.Fatalf("url %q, err %v", url, err)
	}
}

func TestTheDeclarationNamesTheDerivedRefs(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	u, found, err := upstream.Load(f.dir)
	if err != nil || !found {
		t.Fatalf("found %v, err %v", found, err)
	}
	if u.URL() != upstreamURL || u.KeptRef("v0.1.0") != kept+"v0.1.0" || u.BranchRef() != branchRef {
		t.Fatalf("%+v: %s %s %s", u, u.URL(), u.KeptRef("v0.1.0"), u.BranchRef())
	}
}

func TestAnInvalidDeclarationIsRefusedAndTheShapeItPrintsIsAccepted(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	path := filepath.Join(f.dir, ".strictmetadata", "upstream", "upstream.toml")
	testsupport.WriteFile(t, path, "format_version = 1\nhost = \"github.com\"\n")
	_, _, err := upstream.Load(f.dir)
	if err == nil || !strings.Contains(err.Error(), "owner") || !strings.Contains(err.Error(), "branch = \"main\"") {
		t.Fatalf("err %v", err)
	}
	testsupport.WriteFile(t, path, declaration)
	if _, found, err := upstream.Load(f.dir); err != nil || !found {
		t.Fatalf("the declaration the refusal shows was refused: %v", err)
	}
}

func TestADryRunPrintsThePlanAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	local, origin := testsupport.Refs(t, f.dir), testsupport.Refs(t, f.origin)
	r := adopt(t, f.dir, true)
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	for _, want := range []string{"v0.1.0: inherited: at ", "apply would write " + kept + "v0.1.0.", "1 tag(s) the upstream does not have stay in refs/tags: nightly", "Dry run: 3 inherited tag(s) would move"} {
		if !strings.Contains(r.Stdout, want) {
			t.Errorf("the plan lacks %q:\n%s", want, r.Stdout)
		}
	}
	if !equalRefs(local, testsupport.Refs(t, f.dir)) || !equalRefs(origin, testsupport.Refs(t, f.origin)) {
		t.Fatal("the dry run wrote a ref")
	}
}

func TestInheritedTagsMoveKeepingTheirObjects(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	before := refs(t, f.dir, "refs/tags/")
	r := adopt(t, f.dir, false)
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := names(refs(t, f.dir, "refs/tags/")); strings.Join(got, ",") != "refs/tags/nightly" {
		t.Fatalf("local tags: %v", got)
	}
	keptHere := refs(t, f.dir, kept)
	for _, tag := range []string{"v0.1.0", "v0.2.0", "v0.3.0"} {
		if keptHere[kept+tag] != before["refs/tags/"+tag] {
			t.Errorf("%s keeps %q, the tag held %q", tag, keptHere[kept+tag], before["refs/tags/"+tag])
		}
	}
	if typ := f.git("cat-file", "-t", keptHere[kept+"v0.2.0"]); typ != "tag" {
		t.Errorf("the annotated tag's kept ref holds a %s, not the tag object", typ)
	}
	if got := names(refs(t, f.origin, "refs/tags/")); strings.Join(got, ",") != "refs/tags/nightly" {
		t.Fatalf("origin's tags: %v", got)
	}
	if !equalRefs(refs(t, f.origin, kept), keptHere) {
		t.Fatal("origin's kept refs differ from the ones here")
	}
	if !strings.Contains(r.Stdout, "Adopted 3 inherited tag(s)") {
		t.Errorf("summary:\n%s", r.Stdout)
	}
}

func TestASecondRunHasNothingToDo(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	local, origin := testsupport.Refs(t, f.dir), testsupport.Refs(t, f.origin)
	r := adopt(t, f.dir, true)
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "v0.1.0: adopted: already in "+kept+"v0.1.0") || !strings.Contains(r.Stdout, "Nothing to do") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if r := adopt(t, f.dir, false); r.ExitCode != 0 || !strings.Contains(r.Stdout, "Nothing to do") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if !equalRefs(local, testsupport.Refs(t, f.dir)) || !equalRefs(origin, testsupport.Refs(t, f.origin)) {
		t.Fatal("a run with nothing to do wrote a ref")
	}
}

func TestARunFinishesAnInterruptedOne(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	// A crash after the kept refs were written but before any push.
	for _, tag := range []string{"v0.1.0", "v0.2.0", "v0.3.0"} {
		f.git("update-ref", kept+tag, f.git("rev-parse", "refs/tags/"+tag))
	}
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	if got := names(refs(t, f.dir, "refs/tags/")); strings.Join(got, ",") != "refs/tags/nightly" {
		t.Fatalf("local tags: %v", got)
	}
	if got := names(refs(t, f.origin, kept)); len(got) != 3 {
		t.Fatalf("origin's kept refs: %v", got)
	}
}

func TestTagsFetchedBackFromTheUpstreamAreMovedOutAgain(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	f.git("fetch", "-q", "--tags", upstreamURL)
	if _, ok := refs(t, f.dir, "refs/tags/")["refs/tags/v0.1.0"]; !ok {
		t.Fatal("the fetch brought no tag back")
	}
	r := adopt(t, f.dir, false)
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "3 tag(s) deleted from refs/tags here") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := names(refs(t, f.dir, "refs/tags/")); strings.Join(got, ",") != "refs/tags/nightly" {
		t.Fatalf("local tags: %v", got)
	}
}

func TestASameNameTagAtAnotherObjectRefusesTheWholeRun(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	f.git("tag", "-f", "v0.2.0", "HEAD")
	local, origin := testsupport.Refs(t, f.dir), testsupport.Refs(t, f.origin)
	r := adopt(t, f.dir, false)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "v0.2.0: refs/tags/v0.2.0 here is at") || !strings.Contains(r.Stderr, "the upstream's is at") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if !equalRefs(local, testsupport.Refs(t, f.dir)) || !equalRefs(origin, testsupport.Refs(t, f.origin)) {
		t.Fatal("a refused run moved a tag")
	}
}

func TestASameNameTagOnOriginAtAnotherObjectIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	f.git("push", "-q", "-f", "origin", f.git("rev-parse", "HEAD")+":refs/tags/v0.1.0")
	r := adopt(t, f.dir, false)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "v0.1.0: refs/tags/v0.1.0 on origin is at") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if _, ok := refs(t, f.dir, "refs/tags/")["refs/tags/v0.1.0"]; !ok {
		t.Fatal("a refused run deleted a tag")
	}
}

func TestNoDeclarationIsRefusedAndWritingTheShownOneClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	if err := os.Remove(filepath.Join(f.dir, ".strictmetadata", "upstream", "upstream.toml")); err != nil {
		t.Fatal(err)
	}
	r := adopt(t, f.dir, false)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "declares no upstream") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	f.declare()
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("the fix did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestNoOriginIsRefusedAndAddingItClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	url := f.git("remote", "get-url", "origin")
	f.git("remote", "remove", "origin")
	r := adopt(t, f.dir, false)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "has no `origin` remote") || !strings.Contains(r.Stderr, "git remote add origin <url of this fork>") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	f.git("remote", "add", "origin", url)
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("the fix did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestATagOnlyOnOriginWhoseObjectIsMissingNamesItsFetch(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	// A repository that fetched only the fork's main, no tags: the
	// annotated tag's object is not here.
	sparse := testsupport.NewRepo(t)
	sparse.Git("remote", "add", "origin", "file://"+f.origin)
	sparse.Git("fetch", "-q", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
	sparse.Git("config", "url.file://"+f.upstream.Dir+".insteadOf", upstreamURL)
	testsupport.WriteFile(t, sparse.Path(".strictmetadata/upstream/upstream.toml"), declaration)
	r := adopt(t, sparse.Dir, false)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "git fetch origin tag v0.2.0") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	sparse.Git("fetch", "origin", "tag", "v0.2.0")
	if r := adopt(t, sparse.Dir, false); r.ExitCode != 0 {
		t.Fatalf("the fetch did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if got := names(refs(t, f.origin, "refs/tags/")); strings.Join(got, ",") != "refs/tags/nightly" {
		t.Fatalf("origin's tags: %v", got)
	}
}

// pushLogHook is origin's pre-receive hook: it appends one line per push
// holding that push's ref updates, and refuses a push updating a ref named
// in the refuse file, standing in for a push failing part-way through.
const pushLogHook = `#!/bin/sh
log="$1"
refuse="$2"
line=""
refused=0
while read old new ref; do
	line="$line$old $new $ref;"
	if [ -f "$refuse" ] && grep -qx "$ref" "$refuse"; then
		refused=1
	fi
done
printf '%s\n' "$line" >> "$log"
if [ "$refused" = 1 ]; then
	echo "refused by the test's origin" >&2
	exit 1
fi
`

// logPushes installs the push-logging hook on origin and returns the log
// and refuse file paths.
func (f *fork) logPushes() (log, refuse string) {
	f.t.Helper()
	dir := f.t.TempDir()
	log, refuse = filepath.Join(dir, "pushes.txt"), filepath.Join(dir, "refuse")
	script := filepath.Join(dir, "hook.sh")
	if err := os.WriteFile(script, []byte(pushLogHook), 0o755); err != nil {
		f.t.Fatal(err)
	}
	hook := "#!/bin/sh\nexec " + script + " " + log + " " + refuse + "\n"
	if err := os.WriteFile(filepath.Join(f.origin, "hooks", "pre-receive"), []byte(hook), 0o755); err != nil {
		f.t.Fatal(err)
	}
	return log, refuse
}

// pushes are the logged pushes, each a list of "old new ref" updates.
func pushes(t *testing.T, log string) [][]string {
	t.Helper()
	data, err := os.ReadFile(log)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	var out [][]string
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		out = append(out, strings.Split(strings.TrimSuffix(line, ";"), ";"))
	}
	return out
}

func TestEveryPushCarriesOneRef(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	extra := []string{"v0.4.0", "v0.4.1", "v0.4.2"}
	for _, name := range extra {
		f.upstream.CommitFile("extra-"+name+".txt", name+"\n", "upstream: "+name)
		f.upstream.Git("tag", name)
	}
	f.git("fetch", "-q", "--tags", upstreamURL)
	f.git("push", "-q", "origin", "refs/tags/v0.4.0", "refs/tags/v0.4.1", "refs/tags/v0.4.2")
	log, _ := f.logPushes()
	inherited := append([]string{"v0.1.0", "v0.2.0", "v0.3.0"}, extra...)

	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	logged := pushes(t, log)
	if len(logged) != 2*len(inherited) {
		t.Fatalf("%d pushes for %d tags: %v", len(logged), len(inherited), logged)
	}
	order := map[string]int{}
	for i, p := range logged {
		if len(p) != 1 {
			t.Fatalf("push %d carries %d refs: %v", i, len(p), p)
		}
		fields := strings.Fields(p[0])
		order[fields[2]] = i
	}
	for _, tag := range inherited {
		created, okCreated := order[kept+tag]
		deleted, okDeleted := order["refs/tags/"+tag]
		if !okCreated || !okDeleted || created > deleted {
			t.Errorf("%s: its kept ref was not pushed before its tag was deleted from origin (%v)", tag, logged)
		}
	}
}

func TestAFailedPushStopsTheRunAndARunAfterItFinishes(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	_, refuse := f.logPushes()
	if err := os.WriteFile(refuse, []byte("refs/tags/v0.2.0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r := adopt(t, f.dir, false)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "run `rlsbl upstream adopt-tags` again to finish") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	originTags := refs(t, f.origin, "refs/tags/")
	originKept := refs(t, f.origin, kept)
	if _, ok := originTags["refs/tags/v0.1.0"]; ok {
		t.Error("the tag before the failure did not finish")
	}
	if _, ok := originTags["refs/tags/v0.2.0"]; !ok {
		t.Error("the refused deletion happened")
	}
	if _, ok := originKept[kept+"v0.2.0"]; !ok {
		t.Error("the failing tag's kept ref was not pushed before its deletion")
	}
	if _, ok := originKept[kept+"v0.3.0"]; ok {
		t.Error("the tag after the failure was pushed")
	}
	if err := os.Remove(refuse); err != nil {
		t.Fatal(err)
	}
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if got := names(refs(t, f.origin, "refs/tags/")); strings.Join(got, ",") != "refs/tags/nightly" {
		t.Fatalf("origin's tags: %v", got)
	}
	if got := names(refs(t, f.dir, "refs/tags/")); strings.Join(got, ",") != "refs/tags/nightly" {
		t.Fatalf("local tags: %v", got)
	}
}

// exclusions runs HistoryExclusions in dir with a read-only effects handle.
func exclusions(t *testing.T, dir string) ([]string, error) {
	t.Helper()
	var got []string
	var gerr error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		repo, err := git.Open(ctx.Effects(), dir)
		if err != nil {
			gerr = err
			return nil
		}
		got, gerr = upstream.HistoryExclusions(repo)
		return nil
	})
	return got, gerr
}

// runShell runs a printed fix command as it is written (it quotes a refspec
// glob).
func runShell(t *testing.T, dir, command string) {
	t.Helper()
	cmd := exec.Command("sh", "-c", command)
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%s: %v\n%s", command, err, out)
	}
}

func TestExclusionsAreEmptyOutsideAFork(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "a")
	got, err := exclusions(t, repo.Dir)
	if err != nil || len(got) != 0 {
		t.Fatalf("%v, %v", got, err)
	}
}

func TestMissingUpstreamRefsAreRefusedWithTheFetchesThatRestoreThem(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	// A fresh clone: neither the upstream branch ref nor the kept tags.
	for name, object := range refs(t, f.dir, kept) {
		f.git("update-ref", "-d", name, object)
	}
	_, err := exclusions(t, f.dir)
	if err == nil || !strings.Contains(err.Error(), branchRef) {
		t.Fatalf("err %v", err)
	}
	lines := strings.Split(err.Error(), "\n")
	fetches := []string{strings.TrimSpace(lines[len(lines)-2]), strings.TrimSpace(lines[len(lines)-1])}
	want := []string{
		"git fetch --no-tags " + upstreamURL + " +refs/heads/main:" + branchRef,
		"git fetch --no-tags origin '" + kept + "*:" + kept + "*'",
	}
	if fetches[0] != want[0] || fetches[1] != want[1] {
		t.Fatalf("the fetches are %q, want %q", fetches, want)
	}
	for _, command := range fetches {
		runShell(t, f.dir, command)
	}
	got, err := exclusions(t, f.dir)
	if err != nil {
		t.Fatalf("the fetches did not clear the refusal: %v", err)
	}
	if len(got) != 4 || got[0] != branchRef || got[1] != kept+"v0.1.0" {
		t.Fatalf("exclusions: %v", got)
	}
	if tags := names(refs(t, f.dir, "refs/tags/")); strings.Join(tags, ",") != "refs/tags/nightly" {
		t.Fatalf("the fetches brought an inherited tag back: %v", tags)
	}
}

func TestExclusionsLeaveOnlyTheForksOwnCommits(t *testing.T) {
	hygiene.Isolate(t)
	f := newFork(t)
	if r := adopt(t, f.dir, false); r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	u, _, _ := upstream.Load(f.dir)
	runShell(t, f.dir, u.BranchFetchCommand())
	// The upstream's branch moves back, as if rewritten: its tip no longer
	// reaches c.txt's commit, which only the kept v0.3.0 still does.
	f.git("update-ref", branchRef, branchRef+"~1")
	got, err := exclusions(t, f.dir)
	if err != nil {
		t.Fatal(err)
	}
	own := strings.Fields(f.git(append([]string{"rev-list", "HEAD", "--not"}, got...)...))
	if len(own) != 1 || own[0] != f.git("rev-parse", "HEAD") {
		t.Fatalf("the commits left are %v", own)
	}
}
