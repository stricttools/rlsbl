package cli

import (
	"fmt"
	"os"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/runstate"
)

const releaseRunHelp = "Release the releasable the working directory selects (or --releasable, where the directory selects none): bump its " +
	"version from the bump in .strictmetadata/releases/<releasable>/unreleased.toml, commit, push the commit untagged to the release branch as " +
	"the candidate, wait for CI's verdict on it, and only when CI passes finalize the changelog, archive the release with the commit CI " +
	"verified, tag that commit, push, create the GitHub Release, publish the local pipelines, run the deploy_command of a server releasable, " +
	"and run the post-release hooks. Everything before the push runs in the release checkout, a detached checkout of the branch's committed " +
	"tip under the repository's git directory: the pre-checks hooks, the strictcli help document of every member that is a strictcli program, " +
	"selfdoc gen and selfdoc check told the version being released (what selfdoc writes is committed as the release's own commit), the " +
	"preflight checks, the pre-release hooks, the version writes, the build, and its scans. The branch advances only by compare-and-swap from " +
	"where the release left it, and only the files the release's commits change are written into the working tree; an uncommitted change to " +
	"one of them refuses the release, and every other uncommitted change is listed and left alone. Commits on the branch the release did not " +
	"make are refused when it starts mutating, before the candidate push, after the CI verdict, and before the final push. A stop before the " +
	"candidate push discards the attempt; a stop after it keeps the state in .strictmetadata/.release-state/<releasable>/in-progress.toml for " +
	"`" + runstate.ResumeInvocation + "`, and a red CI verdict is fixed forward on the branch and resumed at the same version. --watch watches CI on the " +
	"release commit afterwards and asks the npm and PyPI package listings whether they list the version; --no-watch says the outcome was not " +
	"verified. --dry-run validates, runs the changelog checks and the pure preflight checks, records every program and write up to the release " +
	"commit, and names the steps after it."

const releaseResumeHelp = "Continue the release in progress of the releasable the working directory selects (from a member versioned under " +
	"none, the one releasable with a release in progress) from the step it stopped at. The resume pins again at the branch tip: what was " +
	"committed since the release stopped (the fix after a red CI verdict, other sessions' work) is adopted, provided the changelog describes " +
	"every adopted commit, and refused otherwise before anything is written, naming the `rlsbl changelog add` that records each. A resume " +
	"adopting commits after the CI verdict pushes the tip as the candidate and waits for CI again, so the tag is put on what CI verified; one " +
	"adopting nothing past the verdict tags the commit the state records, which must still be on the branch. Once the changelog is " +
	"finalized, nothing is adopted any more: a resume finding commits it did not make refuses, naming them, until they are off the release " +
	"branch. A failed deploy or post-release " +
	"hook runs again. Before anything is written, the resume is refused what release validation refuses about the lifecycle and " +
	"publishing: a releasable or member on hold or retired, a repository whose visibility disagrees with the lifecycle-and-license record, an " +
	"output the publishing rules forbid, and a deploy command on a releasable that may not deploy. --dry-run reports what would be adopted " +
	"and which steps would run, and writes nothing."

const releaseInitHelp = "Write the release file of the releasable the working directory selects, " +
	".strictmetadata/releases/<releasable>/unreleased.toml, with bump and description blank (a release refuses it until both are filled in), " +
	"context blank, every target of the releasable's members in include, and exclude empty, and commit it. A release file nobody filled in yet " +
	"is left as it is; one somebody filled in is refused, never overwritten. A member versioned under no releasable is refused, naming " +
	"`rlsbl monorepo release init` for a batch release."

// releaseTimeoutFlags are the release's per-invocation timeouts.
func releaseTimeoutFlags() []strictcli.Flag {
	return []strictcli.Flag{
		strictcli.IntFlag("push-timeout", timeoutHelp("Seconds each push may take", "push_seconds", release.ShippedPushTimeout), strictcli.Optional()),
		strictcli.IntFlag("ci-timeout", timeoutHelp("Seconds the wait for CI's verdict on the candidate may take", "ci_seconds", release.ShippedCITimeout), strictcli.Optional()),
		strictcli.IntFlag("check-timeout", timeoutHelp("Seconds each program a check, the schema dump, selfdoc, or the deploy starts may take", "check_seconds", release.ShippedCheckTimeout), strictcli.Optional()),
		strictcli.IntFlag("hook-timeout", "Seconds each hook may take ([timeouts] hook_seconds of releasables.toml when not passed, else no bound)", strictcli.Optional()),
	}
}

// timeoutHelp is the help of a timeout flag: what it bounds, and the
// declaration and the shipped value it falls back to.
func timeoutHelp(bounds, key string, shipped time.Duration) string {
	return fmt.Sprintf("%s ([timeouts] %s of releasables.toml when not passed, else %d)", bounds, key, int64(shipped/time.Second))
}

func registerReleaseRun(r *commandSet, version string) {
	r.add(command{
		path:   []string{"release", "run"},
		help:   releaseRunHelp,
		effect: mutating,
		// A release tags, publishes, and deploys a version: only a person
		// decides that.
		consequential: true,
		flags: append([]strictcli.Flag{
			strictcli.BoolFlag("watch", "Watch CI on the release commit afterwards and verify the registries list the version (--no-watch says it was not verified)", strictcli.Required()),
			strictcli.StringFlag("releasable", "The releasable to release, where the working directory selects none", strictcli.Optional()),
		}, releaseTimeoutFlags()...),
		run: func(ctx *strictcli.Context, kw map[string]any) (any, error) {
			req, err := releaseRunRequest(ctx, kw, version, r.app)
			if err != nil {
				return nil, err
			}
			named, given := strictcli.GetOpt[string](kw, "releasable")
			if given && named == "" {
				return nil, emptyArgument("--releasable")
			}
			req.Releasable = named
			return nil, release.Run(ctx.Effects(), req)
		},
	})
	r.add(command{
		path:   []string{"release", "resume"},
		help:   releaseResumeHelp,
		effect: mutating,
		// A resume tags, publishes, and deploys what the release it continues
		// would have: only a person decides that.
		consequential: true,
		flags: append([]strictcli.Flag{
			strictcli.BoolFlag("watch", "Watch CI on the release commit afterwards and verify the registries list the version (--no-watch says it was not verified)", strictcli.Required()),
		}, releaseTimeoutFlags()...),
		run: func(ctx *strictcli.Context, kw map[string]any) (any, error) {
			req, err := releaseRunRequest(ctx, kw, version, r.app)
			if err != nil {
				return nil, err
			}
			return nil, release.Resume(ctx.Effects(), req)
		},
	})
	r.add(command{
		path:     []string{"release", "init"},
		help:     releaseInitHelp,
		effect:   mutating,
		noDryRun: "it writes one scaffolded file and commits it, and the file it would write is what this help describes",
		run: func(ctx *strictcli.Context, _ map[string]any) (any, error) {
			dir, root, err := workingRepository()
			if err != nil {
				return nil, err
			}
			res, err := release.Init(ctx.Effects(), root, dir)
			if err != nil {
				return nil, err
			}
			if res.Written {
				ctx.Out("Wrote and committed " + res.Path + "; fill in bump and description, then run `" + runstate.RunInvocation + "`.")
			} else {
				ctx.Out(res.Path + " exists and nobody filled it in yet; nothing was written.")
			}
			return nil, nil
		},
	})
}

// releaseRunRequest is the request `release run` and `release resume`
// share: the working directory, the flags, and the inputs from this machine.
func releaseRunRequest(ctx *strictcli.Context, kw map[string]any, version string, checks release.CheckRunner) (release.RunRequest, error) {
	dir, root, err := workingRepository()
	if err != nil {
		return release.RunRequest{}, err
	}
	fork, err := forkHistory(ctx.Effects(), root)
	if err != nil {
		return release.RunRequest{}, err
	}
	indexPath, err := index.DefaultPath()
	if err != nil {
		return release.RunRequest{}, err
	}
	req := release.RunRequest{
		Dir:          dir,
		LiveRoot:     root,
		DryRun:       ctx.DryRun(),
		Watch:        strictcli.Get[bool](kw, "watch"),
		Fork:         fork,
		RlsblVersion: version,
		Checks:       checks,
		IndexPath:    indexPath,
		Now:          time.Now,
		Sleep:        time.Sleep,
		Log:          ctx.Info,
		Warn:         ctx.Warn,
	}
	// The preflight checks read files under the home directory; a release
	// that cannot name it does not run them against nothing.
	home, err := os.UserHomeDir()
	if err != nil {
		return release.RunRequest{}, fmt.Errorf("the home directory, whose files the release's checks read, cannot be found: %w", err)
	}
	req.Home = home
	t := &req.Timeouts
	t.Push, t.PushGiven = strictcli.GetOpt[int](kw, "push_timeout")
	t.CI, t.CIGiven = strictcli.GetOpt[int](kw, "ci_timeout")
	t.Check, t.CheckGiven = strictcli.GetOpt[int](kw, "check_timeout")
	t.Hook, t.HookGiven = strictcli.GetOpt[int](kw, "hook_timeout")
	return req, nil
}
