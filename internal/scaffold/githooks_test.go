package scaffold

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// checkTagHook is the namespace-aware pre-push hook rlsbl installed before
// the hook ran failing-checks: it ran the whole check report on the prepush
// tag, so a warning blocked the push.
const checkTagHook = `#!/usr/bin/env bash
# rlsbl pre-push hook. Enforces on refs/heads/* only; refs/tags/* and
# refs/backups/* are tool-owned namespaces and exit 0.
stdin_data="$(cat)"
enforce=0
while read -r _local_ref _local_sha remote_ref _remote_sha; do
  case "$remote_ref" in
    refs/heads/*) enforce=1 ;;
  esac
done <<< "$stdin_data"
if [ "$enforce" -eq 0 ]; then
  exit 0
fi
export RLSBL_PUSH_STDIN="$stdin_data"
exec rlsbl check --tag prepush
`

func TestHookHashesIgnoreTrailingWhitespace(t *testing.T) {
	hygiene.Isolate(t)
	if HookHash("a\n") != HookHash("a") || HookHash("a\n\n  ") != HookHash("a") {
		t.Fatal("trailing whitespace changed the hash")
	}
	if HookHash("a") == HookHash("b") {
		t.Fatal("different content hashed the same")
	}
	if !ShippedHook("pre-push", PrePushHook) || !ShippedHook("post-rewrite", PostRewriteHook) {
		t.Fatal("the current hooks are not recognized as shipped")
	}
	if !ShippedHook("pre-push", checkTagHook) || !ShippedHook("pre-push", "#!/usr/bin/env bash\nexec rlsbl pre-push-check\n") {
		t.Fatal("an earlier shipped pre-push hook is not recognized")
	}
	if len(shippedPrePushHooks) < 7 {
		t.Fatalf("only %d shipped pre-push hooks are known", len(shippedPrePushHooks))
	}
	if ShippedHook("pre-push", "#!/usr/bin/env bash\necho mine\n") {
		t.Fatal("a hook rlsbl never shipped is recognized")
	}
}

func TestTheHooksRunTheirDeclaredCommands(t *testing.T) {
	hygiene.Isolate(t)
	if !strings.Contains(PrePushHook, "exec rlsbl failing-checks --hook pre-push\n") || strings.Contains(PrePushHook, "--tag") {
		t.Fatalf("the pre-push hook runs something else:\n%s", PrePushHook)
	}
	if !strings.Contains(PostRewriteHook, "exec rlsbl changelog remap --stdin") {
		t.Fatalf("the post-rewrite hook runs something else:\n%s", PostRewriteHook)
	}
}

// runHook runs hook content with bash, a fake rlsbl first on PATH that
// prints its arguments, the push variable, and its stdin.
func runHook(t *testing.T, content, stdin string) string {
	t.Helper()
	dir := t.TempDir()
	bin := filepath.Join(dir, "bin")
	testsupport.WriteFile(t, filepath.Join(bin, "rlsbl"), "#!/usr/bin/env bash\necho \"ARGS=$*\"\necho \"PUSH=$RLSBL_PUSH_STDIN\"\necho \"STDIN=$(cat)\"\n")
	if err := os.Chmod(filepath.Join(bin, "rlsbl"), 0o755); err != nil {
		t.Fatal(err)
	}
	hook := filepath.Join(dir, "hook")
	testsupport.WriteFile(t, hook, content)
	if out, err := exec.Command("bash", "-n", hook).CombinedOutput(); err != nil {
		t.Fatalf("bash -n: %v\n%s", err, out)
	}
	cmd := exec.Command("bash", hook)
	cmd.Env = append(os.Environ(), "PATH="+bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	cmd.Stdin = strings.NewReader(stdin)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("the hook failed: %v\n%s", err, out)
	}
	return string(out)
}

func TestThePrePushHookChecksBranchPushesOnly(t *testing.T) {
	hygiene.Isolate(t)
	push := "refs/heads/main aaa111 refs/heads/main bbb222"
	out := runHook(t, PrePushHook, push+"\n")
	if !strings.Contains(out, "ARGS=failing-checks --hook pre-push") || !strings.Contains(out, "PUSH="+push) {
		t.Fatalf("a branch push:\n%s", out)
	}
	if out := runHook(t, PrePushHook, "refs/tags/v1.0.0 aaa111 refs/tags/v1.0.0 0000000\n"); strings.Contains(out, "ARGS=") {
		t.Fatalf("a tag push ran the checks:\n%s", out)
	}
}

func TestThePostRewriteHookPipesTheMapToTheRemap(t *testing.T) {
	hygiene.Isolate(t)
	out := runHook(t, PostRewriteHook, "old_abc new_def\nold_111 new_222\n")
	if !strings.Contains(out, "ARGS=changelog remap --stdin") || !strings.Contains(out, "old_abc new_def") || !strings.Contains(out, "old_111 new_222") {
		t.Fatalf("output:\n%s", out)
	}
}

// installInto plans and installs the hooks of the repository at dir and
// returns what was said; the plan's error ends the command.
func installInto(t *testing.T, dir string) (said []string, result strictcli.Result) {
	t.Helper()
	result = testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		hooks, err := HooksDirectory(ctx.Effects(), dir)
		if err != nil {
			return err
		}
		changes, err := planHooks(hooks)
		if err != nil {
			return err
		}
		return installHooks(ctx.Effects(), changes, func(s string) { said = append(said, s) })
	})
	return said, result
}

func TestHooksAreInstalledKeptAndUpgraded(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	prePush := repo.Path(".git/hooks/pre-push")
	said, r := installInto(t, repo.Dir)
	if r.ExitCode != 0 || len(said) != 2 || !strings.HasPrefix(said[0], "Installed pre-push hook") || !strings.Contains(said[0], prePush) {
		t.Fatalf("a fresh install: %q\n%s", said, r.Stderr)
	}
	info, err := os.Stat(prePush)
	if err != nil || info.Mode().Perm()&0o100 == 0 {
		t.Fatalf("the hook is not executable: %v", err)
	}
	if said, r := installInto(t, repo.Dir); r.ExitCode != 0 || len(said) != 0 {
		t.Fatalf("current hooks were rewritten: %q", said)
	}
	testsupport.WriteFile(t, prePush, checkTagHook)
	said, r = installInto(t, repo.Dir)
	if r.ExitCode != 0 || len(said) != 1 || !strings.HasPrefix(said[0], "Updated pre-push hook") {
		t.Fatalf("an earlier hook: %q\n%s", said, r.Stderr)
	}
	if data, _ := os.ReadFile(prePush); string(data) != PrePushHook {
		t.Fatalf("the hook was not upgraded:\n%s", data)
	}
}

func TestAHookRlsblNeverShippedIsRefusedUntilDeleted(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	custom := "#!/usr/bin/env bash\n# mine\necho doing my thing\n"
	prePush := repo.Path(".git/hooks/pre-push")
	testsupport.WriteFile(t, prePush, custom)
	said, r := installInto(t, repo.Dir)
	if r.ExitCode == 0 || len(said) != 0 || !strings.Contains(r.Stderr, "not one rlsbl installed") || !strings.Contains(r.Stderr, "saferm delete") {
		t.Fatalf("a customized hook: %q\n%s", said, r.Stderr)
	}
	if data, _ := os.ReadFile(prePush); string(data) != custom {
		t.Fatal("the customized hook was touched")
	}
	if _, err := os.Stat(repo.Path(".git/hooks/post-rewrite")); err == nil {
		t.Fatal("a refused plan still installed the other hook")
	}
	// The fix the refusal names: delete the hook.
	if err := os.Remove(prePush); err != nil {
		t.Fatal(err)
	}
	if said, r := installInto(t, repo.Dir); r.ExitCode != 0 || len(said) != 2 {
		t.Fatalf("after deleting it: %q\n%s", said, r.Stderr)
	}
}

func TestAHooksPathElsewhereIsRefusedAndUnsettingItClears(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Git("config", "core.hooksPath", ".githooks")
	_, r := installInto(t, repo.Dir)
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, ".githooks") || !strings.Contains(r.Stderr, "git config --file .git/config --unset core.hooksPath") {
		t.Fatalf("stderr:\n%s", r.Stderr)
	}
	if _, err := os.Stat(repo.Path(".githooks")); err == nil {
		t.Fatal("the refusal wrote into the hooks path")
	}
	repo.Git("config", "--file", ".git/config", "--unset", "core.hooksPath")
	if _, r := installInto(t, repo.Dir); r.ExitCode != 0 {
		t.Fatalf("after unsetting it:\n%s", r.Stderr)
	}
}

func TestAHooksPathNamingTheRepositorysOwnHooksIsAccepted(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Git("config", "core.hooksPath", repo.Path(".git/hooks"))
	if _, r := installInto(t, repo.Dir); r.ExitCode != 0 {
		t.Fatalf("stderr:\n%s", r.Stderr)
	}
	if data, _ := os.ReadFile(repo.Path(".git/hooks/pre-push")); string(data) != PrePushHook {
		t.Fatal("the hook was not installed")
	}
}

func TestALinkedWorktreeInstallsIntoTheCommonHooks(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("README.md", "x\n", "init")
	linked := filepath.Join(t.TempDir(), "linked")
	repo.Git("worktree", "add", "-q", "--detach", linked)
	said, r := installInto(t, linked)
	common := repo.Path(".git/hooks/pre-push")
	if r.ExitCode != 0 || len(said) == 0 || !strings.Contains(said[0], common) {
		t.Fatalf("said %q\n%s", said, r.Stderr)
	}
	if data, _ := os.ReadFile(common); string(data) != PrePushHook {
		t.Fatal("the common hooks directory has no pre-push hook")
	}
}
