// Package declarations reads and writes a repository's release declarations,
// .strictmetadata/releasables/releasables.toml, and the sandboxed test
// runner's settings, .strictmetadata/test-runner/test-runner.toml.
//
// Reading is strict and happens in three passes, each refusing what the
// previous one cannot see: the strictspec-generated validator (the document's
// shape: every key at every level, value types, closed value sets), a strict
// typed decode, and the declaration rules that relate one part of the
// document to another (the root member, a member naming a declared
// releasable, a pipeline naming a target its member declares). Every problem
// a pass finds is reported at once, each naming the table it is in.
//
// Nothing is derived on read: a releasable's name and tag format, a member's
// name, and whether the repository is a workspace are what the file says,
// and a file that does not say is refused.
//
// Writing edits the existing document in place (comments, key order, and
// every untouched table keep their bytes), validates the result with the same
// three passes, and writes it through the strictcli effects handle.
//
// The paths of every record rlsbl keeps under .strictmetadata/ are declared
// here, once, so every package names them the same way.
package declarations

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/stricttools/rlsbl/internal/git"
)

// MetadataDir is the directory at a repository's root that holds every
// family tool's records; the git package declares it, since its commits
// carry the ownership manifests of the directories under it.
const MetadataDir = git.MetadataDir

// The records rlsbl keeps, as repository-relative slash-separated paths.
const (
	// ReleasablesDir holds the release declarations and its manifest.
	ReleasablesDir = MetadataDir + "/releasables"
	// ReleasablesFile is the release declarations.
	ReleasablesFile = ReleasablesDir + "/releasables.toml"
	// TestRunnerDir holds the test runner's settings and its manifest.
	TestRunnerDir = MetadataDir + "/test-runner"
	// TestRunnerFile is the sandboxed test runner's settings.
	TestRunnerFile = TestRunnerDir + "/test-runner.toml"
	// ChangelogRoot holds one changelog directory per releasable.
	ChangelogRoot = MetadataDir + "/changelog"
	// ReleasesRoot holds one release directory per releasable.
	ReleasesRoot = MetadataDir + "/releases"
	// BatchReleasesDir holds the batch release file and its archives.
	BatchReleasesDir = MetadataDir + "/batch-releases"
	// TransitionsFile is the transition record.
	TransitionsFile = MetadataDir + "/transitions/transitions.jsonl"
	// HistoryRewritesDir holds one archive per history rewrite.
	HistoryRewritesDir = MetadataDir + "/history-rewrites"
	// RetiredHistoriesRoot holds the release state of each retired subject.
	RetiredHistoriesRoot = MetadataDir + "/retired-release-histories"
	// ReleaseHooksRoot holds the hook scripts declarations run.
	ReleaseHooksRoot = MetadataDir + "/release-hooks"
	// ScaffoldStateFile is the scaffold state.
	ScaffoldStateFile = MetadataDir + "/.scaffold-state/scaffold-state.toml"
	// ScaffoldBasesDir holds the three-way merge bases.
	ScaffoldBasesDir = MetadataDir + "/.scaffold-bases"
	// ChangelogValidationDir holds one validation cache per releasable.
	ChangelogValidationDir = MetadataDir + "/.changelog-validation"
	// ReleaseStateDir holds the run state of releases in progress.
	ReleaseStateDir = MetadataDir + "/.release-state"
	// PrivateModuleFile is the go.mod that keeps .strictmetadata/ out of a
	// Go module.
	PrivateModuleFile = MetadataDir + "/go.mod"
)

// ChangelogDir is a releasable's changelog directory.
func ChangelogDir(releasable string) string { return ChangelogRoot + "/" + releasable }

// ReleasesDir is a releasable's release directory: its release file, its
// archives, its version file, and its undo audits.
func ReleasesDir(releasable string) string { return ReleasesRoot + "/" + releasable }

// VersionFile is a workspace releasable's version file.
func VersionFile(releasable string) string { return ReleasesDir(releasable) + "/version" }

// ChangelogValidationFile is a releasable's changelog validation cache.
func ChangelogValidationFile(releasable string) string {
	return ChangelogValidationDir + "/" + releasable + ".toml"
}

// RunStateDir is the run state directory of a releasable's release.
func RunStateDir(releasable string) string { return ReleaseStateDir + "/" + releasable }

// RetiredHistoryDir is the release state of a retired subject.
func RetiredHistoryDir(subject string) string { return RetiredHistoriesRoot + "/" + subject }

// ReleaseHooksDir holds the hook scripts of one releasable or member.
func ReleaseHooksDir(owner string) string { return ReleaseHooksRoot + "/" + owner }

// FindRepositoryRoot is the root of the git working tree that contains start:
// start itself or its nearest ancestor that is a repository root. A
// repository's declarations sit at its own root, so an enclosing
// repository's declarations are never a nested repository's. A start outside
// every repository is refused.
func FindRepositoryRoot(start string) (string, error) {
	abs, err := filepath.Abs(start)
	if err != nil {
		return "", fmt.Errorf("resolving %s: %w", start, err)
	}
	dir, err := filepath.EvalSymlinks(abs)
	if err != nil {
		return "", fmt.Errorf("resolving %s: %w", start, err)
	}
	for {
		if git.IsRepositoryRoot(dir) {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("%s is not inside a git repository: rlsbl reads release declarations from %s at a repository's root", abs, ReleasablesFile)
		}
		dir = parent
	}
}

// readRecord reads the record at rel under the repository root. A missing
// file is reported with found false and no error.
func readRecord(root, rel string) (data []byte, found bool, err error) {
	data, err = os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", rel, err)
	}
	return data, true, nil
}
