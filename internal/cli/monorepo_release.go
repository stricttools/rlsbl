package cli

import (
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/batchrelease"
	"github.com/stricttools/rlsbl/internal/runstate"
)

const monorepoReleaseHelp = "Release several releasables of a workspace as one batch: write the batch release file, release what it names, and report the order a batch releases them in."

const monorepoReleaseRunHelp = "Release the releasables the batch release file .strictmetadata/batch-releases/unreleased.toml names, one " +
	"[releasables.<name>] table each with the fields of a release file, in the order `rlsbl monorepo release order` reports: each after the " +
	"releasables its members depend on. The whole batch runs in the release checkout, as `" + runstate.RunInvocation + "` does: an uncommitted change to a path " +
	"the batch writes refuses it, naming the path, and every other uncommitted change is listed and left alone. The first run validates every " +
	"releasable before anything is written and plans the batch (the version and tag each one ships) in .strictmetadata/.release-state/batch-plan.toml. " +
	"Each releasable is then released up to its release commit; the branch tip, holding every release commit, is pushed untagged as one candidate, " +
	"and CI's verdict on it is awaited once, every releasable's own CI jobs required to have run. Only when CI passes is each releasable finished on " +
	"that commit: its changelog finalized, its release archived, tagged, pushed, its GitHub Release created, its local pipelines published, its " +
	"deploy_command run, and its post-release hooks run. A run after a stop follows the plan: a releasable released already is skipped, one committed " +
	"short of the CI verdict joins the new candidate (a red verdict is fixed forward on the branch and continued at the same versions), and one past " +
	"the verdict is refused, naming `" + runstate.ResumeInvocation + "`. The run releasing the last releasable archives the batch release file as " +
	".strictmetadata/batch-releases/batch-<UTC time>.toml, commits and pushes it, and removes the plan. A root member versioned under no releasable " +
	"that declares selfdoc.json gets selfdoc gen and selfdoc check first. --watch and --no-watch apply to each releasable as in `" + runstate.RunInvocation + "`. " +
	"--dry-run validates every releasable and reports the versions, tags, and order, writing nothing."

const monorepoReleaseInitHelp = "Write the batch release file .strictmetadata/batch-releases/unreleased.toml with one [releasables.<name>] table " +
	"per releasable (the ones --releasables names, once per releasable, or every declared one), each with bump and description blank (a batch " +
	"release refuses it until both are filled in), context blank, every target of the releasable's members in include, and exclude empty, and " +
	"commit it. A releasable with no commit needing a changelog entry since its latest release is written commented out, and one with no target is " +
	"refused. A batch release file nobody filled in yet is left as it is; one somebody filled in is refused, never overwritten."

const monorepoReleaseOrderHelp = "Report every member of the workspace, each after the members it depends on (its manifests' dependencies on " +
	"other members and its depends_on), and every releasable in the order a batch release releases them: by the latest position any of its members " +
	"takes, ties by name. A manifest that cannot be read and a dependency cycle are refused."

func registerMonorepoRelease(r *commandSet, version string) {
	r.group([]string{"monorepo", "release"}, monorepoReleaseHelp)
	r.add(command{
		path:   []string{"monorepo", "release", "run"},
		help:   monorepoReleaseRunHelp,
		effect: mutating,
		// A batch release tags, publishes, and deploys a version of every
		// releasable it names: only a person decides that.
		consequential: true,
		flags: append([]strictcli.Flag{
			strictcli.BoolFlag("watch", "Watch CI on each release commit afterwards and verify the registries list each version (--no-watch says it was not verified)", strictcli.Required()),
		}, releaseTimeoutFlags()...),
		run: func(ctx *strictcli.Context, kw map[string]any) (any, error) {
			req, err := releaseRunRequest(ctx, kw, version, r.app)
			if err != nil {
				return nil, err
			}
			return nil, batchrelease.Run(ctx.Effects(), req)
		},
	})
	r.add(command{
		path:     []string{"monorepo", "release", "init"},
		help:     monorepoReleaseInitHelp,
		effect:   mutating,
		noDryRun: "it writes one scaffolded file and commits it, and the file it would write is what this help describes",
		flags: []strictcli.Flag{
			strictcli.StringFlag("releasables", "A releasable to write a table for; repeatable (every declared releasable when not passed)", strictcli.Optional(), strictcli.Repeatable(), strictcli.Unique(true)),
		},
		run: runMonorepoReleaseInit,
	})
	r.add(command{
		path:    []string{"monorepo", "release", "order"},
		help:    monorepoReleaseOrderHelp,
		effect:  readOnly,
		payload: releaseOrderPayloadSchema(),
		render:  renderReleaseOrder,
		run: func(_ *strictcli.Context, _ map[string]any) (any, error) {
			dir, _, err := workingRepository()
			if err != nil {
				return nil, err
			}
			return batchrelease.Order(dir)
		},
	})
}

func runMonorepoReleaseInit(ctx *strictcli.Context, kw map[string]any) (any, error) {
	_, root, err := workingRepository()
	if err != nil {
		return nil, err
	}
	names, err := optionalStrings(kw, "releasables")
	if err != nil {
		return nil, err
	}
	for _, n := range names {
		if n == "" {
			return nil, emptyArgument("--releasables")
		}
	}
	fork, err := forkHistory(ctx.Effects(), root)
	if err != nil {
		return nil, err
	}
	res, err := batchrelease.Init(ctx.Effects(), root, names, fork)
	if err != nil {
		return nil, err
	}
	if !res.Written {
		ctx.Out(res.Path + " exists and nobody filled it in yet; nothing was written.")
		return nil, nil
	}
	msg := "Wrote and committed " + res.Path + " with a table for " + strings.Join(res.Releasables, ", ")
	if len(res.Idle) > 0 {
		msg += " (commented out, with nothing to release: " + strings.Join(res.Idle, ", ") + ")"
	}
	ctx.Out(msg + "; fill in each table's bump and description, then run `" + runstate.BatchRunInvocation + "`.")
	return nil, nil
}

func releaseOrderPayloadSchema() map[string]any {
	return objectSchema(map[string]any{
		"members":     stringArraySchema(),
		"independent": booleanSchema(),
		"releasables": stringArraySchema(),
	})
}

func renderReleaseOrder(payload any) string {
	report, err := decodePayload[batchrelease.OrderReport](payload)
	if err != nil {
		return renderJSON(payload)
	}
	return report.Render()
}
