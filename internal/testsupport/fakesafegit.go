package testsupport

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// fakeSafegitScript performs `safegit commit [--trailer <t>] -m <message> --
// <paths>` with git and refuses every other use, recording each argv.
const fakeSafegitScript = `#!/bin/sh
printf '%s\n' "$*" >> "${0%/*}/safegit-calls.txt"
if [ "$1" != commit ]; then
	echo "fake safegit: only commit is faked" >&2
	exit 97
fi
shift
trailer=""
message=""
while [ $# -gt 0 ]; do
	case "$1" in
	--trailer) trailer="$2"; shift 2 ;;
	-m) message="$2"; shift 2 ;;
	--) shift; break ;;
	*) echo "fake safegit: unexpected argument $1" >&2; exit 97 ;;
	esac
done
git add -- "$@" || exit 1
if [ -n "$trailer" ]; then
	exec git commit -q --trailer "$trailer" -m "$message"
fi
exec git commit -q -m "$message"
`

// Safegit is a fake safegit installed for one test.
type Safegit struct {
	t   testing.TB
	dir string
}

// FakeSafegit puts a safegit first on PATH for the rest of the test: a shell
// script that performs `safegit commit` with git add and git commit and
// refuses every other subcommand.
func FakeSafegit(t testing.TB) *Safegit {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "safegit"), []byte(fakeSafegitScript), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return &Safegit{t: t, dir: dir}
}

// Calls is the argv of every invocation, space-joined, in order.
func (s *Safegit) Calls() []string {
	s.t.Helper()
	data, err := os.ReadFile(filepath.Join(s.dir, "safegit-calls.txt"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		s.t.Fatal(err)
	}
	return strings.Split(strings.TrimRight(string(data), "\n"), "\n")
}

// PathOnly sets PATH, for the rest of the test, to one fresh directory
// holding a link to each named program as the current PATH finds it, so
// every other program is missing.
func PathOnly(t testing.TB, programs ...string) {
	t.Helper()
	dir := t.TempDir()
	for _, p := range programs {
		path, err := exec.LookPath(p)
		if err != nil {
			t.Fatalf("testsupport: %s is not on PATH: %v", p, err)
		}
		if err := os.Symlink(path, filepath.Join(dir, p)); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir)
}

// Saferm is a fake saferm installed for one test.
type Saferm struct {
	t     testing.TB
	calls string
}

// FakeSaferm puts a saferm first on PATH for the rest of the test: a shell
// script that records its argv and deletes nothing.
func FakeSaferm(t testing.TB) *Saferm {
	t.Helper()
	dir := t.TempDir()
	calls := filepath.Join(dir, "saferm-calls.txt")
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"${0%/*}/saferm-calls.txt\"\n"
	if err := os.WriteFile(filepath.Join(dir, "saferm"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return &Saferm{t: t, calls: calls}
}

// Calls is the argv of every invocation, space-joined, in order.
func (s *Saferm) Calls() []string {
	s.t.Helper()
	data, err := os.ReadFile(s.calls)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		s.t.Fatal(err)
	}
	return strings.Split(strings.TrimRight(string(data), "\n"), "\n")
}
