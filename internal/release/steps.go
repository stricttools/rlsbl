package release

import "fmt"

// The steps a release records in its in-progress state, in the order it
// walks them. The names are the state file's format.
const (
	StepVersionBumped        = "version-bumped"
	StepCommitted            = "committed"
	StepCandidatePushed      = "candidate-pushed"
	StepCIVerified           = "ci-verified"
	StepChangelogFinalized   = "changelog-finalized"
	StepReleaseArchived      = "release-archived"
	StepTagged               = "tagged"
	StepPushed               = "pushed"
	StepGitHubReleaseCreated = "github-release-created"
	StepPipelinesPublished   = "pipelines-published"
	StepDeployed             = "deployed"
	StepPostReleaseHooksRun  = "post-release-hooks-run"
)

// EntryType is one piece of work a step's plan issues; several of them make
// up one recorded step.
type EntryType string

// The plan entry types of the version-bumped step, in the order its plan
// issues them.
const (
	EntryWriteReleasableVersion EntryType = "write-releasable-version"
	EntryWriteTargetVersions    EntryType = "write-target-versions"
	EntryWriteMemberVersions    EntryType = "write-member-versions"
	EntryBumpSelfdoc            EntryType = "bump-selfdoc"
	EntryEnsureKeyword          EntryType = "ensure-keyword"
	EntrySyncLockfiles          EntryType = "sync-lockfiles"
	EntryWriteScaffoldState     EntryType = "write-scaffold-state"
	EntryCleanArtifacts         EntryType = "clean-artifacts"
	EntryBuild                  EntryType = "build"
	EntrySecretScan             EntryType = "secret-scan"
	EntryPackedContents         EntryType = "packed-artifact-contents"
	EntryGuardUnexpectedFiles   EntryType = "guard-unexpected-files"
)

// Step is one step a release records.
type Step struct {
	Name string
	// Fatal is whether a failure of the step stops the release.
	Fatal bool
	// Entries are the plan entry types that issue the step, in order; none
	// for a step the release issues directly.
	Entries []EntryType
}

// Steps is the release's step table, in the order a release walks it. Every
// other list of steps is derived from it: the run state's questions
// (StepNames, FatalSteps), and the plan entries a step's plan may hold.
var Steps = []Step{
	{Name: StepVersionBumped, Fatal: true, Entries: []EntryType{
		EntryWriteReleasableVersion,
		EntryWriteTargetVersions,
		EntryWriteMemberVersions,
		EntryBumpSelfdoc,
		EntryEnsureKeyword,
		EntrySyncLockfiles,
		EntryWriteScaffoldState,
		EntryCleanArtifacts,
		EntryBuild,
		EntrySecretScan,
		EntryPackedContents,
		EntryGuardUnexpectedFiles,
	}},
	{Name: StepCommitted, Fatal: true},
	{Name: StepCandidatePushed, Fatal: true},
	{Name: StepCIVerified, Fatal: true},
	{Name: StepChangelogFinalized, Fatal: true},
	{Name: StepReleaseArchived, Fatal: true},
	{Name: StepTagged, Fatal: true},
	{Name: StepPushed, Fatal: true},
	{Name: StepGitHubReleaseCreated, Fatal: true},
	{Name: StepPipelinesPublished, Fatal: true},
	{Name: StepDeployed, Fatal: true},
	{Name: StepPostReleaseHooksRun, Fatal: false},
}

// StepNames are the step names in order, the list the run state's
// questions take.
func StepNames() []string {
	names := make([]string, len(Steps))
	for i, s := range Steps {
		names[i] = s.Name
	}
	return names
}

// FatalSteps are the fatal steps, the set the run state's questions take.
func FatalSteps() map[string]bool {
	fatal := map[string]bool{}
	for _, s := range Steps {
		if s.Fatal {
			fatal[s.Name] = true
		}
	}
	return fatal
}

// StepNamed is the step of that name, and false when the table holds none.
func StepNamed(name string) (Step, bool) {
	for _, s := range Steps {
		if s.Name == name {
			return s, true
		}
	}
	return Step{}, false
}

// stepOfEntry is the step whose plan issues the entry type.
func stepOfEntry(t EntryType) (string, error) {
	for _, s := range Steps {
		for _, e := range s.Entries {
			if e == t {
				return s.Name, nil
			}
		}
	}
	return "", fmt.Errorf("no release step issues the plan entry %q", t)
}
