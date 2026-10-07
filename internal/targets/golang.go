package targets

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
)

// VersionFile is the file a Go project's version lives in: Go modules carry
// no version of their own.
const VersionFile = "VERSION"

// goTarget is a Go module with a VERSION file.
type goTarget struct{}

func (goTarget) Name() string { return Go }

func (goTarget) Facts() Facts {
	return Facts{
		Name:                         Go,
		Ecosystem:                    "Go modules",
		DetectionFiles:               []string{gomodule.FileName},
		VersionFiles:                 []string{VersionFile},
		CompanionTag:                 "{path}/v{version}, or v{version} at the repository root",
		RegistryDisplayName:          "pkg.go.dev",
		ProjectInitHint:              "Run \"go mod init <module-path>\" first",
		ReleaseMaterializationPolicy: MaterializeUnlessIdentityChanged,
		PackageRename:                RenameGoModulePath,
		PackageNameField:             "go.mod module directive (its last path element)",
		BuiltinTestCommand:           strings.Join(goTestArgv, " "),
		TestSettings:                 []string{"go_command"},
		DevInstallGlobal:             "go install <install_paths>",
		SupportsDepFloors:            true,
		ListsUploadOffline:           true,
		ScratchTestExclusion:         ScratchGoNestedModule,
	}
}

func (goTarget) Detect(dir string) (bool, error) {
	return exists(filepath.Join(dir, gomodule.FileName))
}

// exists reports whether path exists; an error other than its absence is an
// error.
func exists(path string) (bool, error) {
	_, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

func (goTarget) ReadVersion(dir string) (semver.Version, error) {
	path := filepath.Join(dir, VersionFile)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return semver.Version{}, fmt.Errorf("%s holds no %s file, which a Go project's version lives in: create it holding the project's current version (`rlsbl scaffold` writes it in a project it scaffolds) and commit it", dir, VersionFile)
	}
	if err != nil {
		return semver.Version{}, fmt.Errorf("reading %s: %w", path, err)
	}
	v, err := semver.Parse(strings.TrimSpace(string(data)))
	if err != nil {
		return semver.Version{}, fmt.Errorf("%s: %w", path, err)
	}
	return v, nil
}

func (goTarget) WriteVersion(w Writer, dir string, v semver.Version) ([]string, error) {
	if err := replaceFile(w, filepath.Join(dir, VersionFile), []byte(v.String()+"\n")); err != nil {
		return nil, err
	}
	return []string{VersionFile}, nil
}

// ReadName is the module path's last element, the name a Go consumer
// resolves the module by.
func (goTarget) ReadName(dir string) (string, bool, error) {
	path, found, err := gomodule.ModulePath(dir)
	if err != nil || !found {
		return "", found, err
	}
	return gomodule.LastElement(path), true, nil
}

// ReadMetadata is empty: go.mod states no license or description.
func (goTarget) ReadMetadata(string) (Metadata, error) { return Metadata{}, nil }

// NormalizePackageName compares the last element of a module path,
// lowercased.
func (goTarget) NormalizePackageName(name string) string {
	return strings.ToLower(gomodule.LastElement(name))
}

// PackageNameProblems refuses anything the offline Go package-name judgment
// does not call available; a discouraged name names the clean spelling
// when lowercasing it and dropping its underscores gives an available one.
func (goTarget) PackageNameProblems(name string) []string {
	verdict := registry.JudgeGoPackageName(name)
	switch verdict.Status {
	case registry.StatusAvailable:
		return nil
	case registry.StatusDiscouraged:
	default:
		return []string{fmt.Sprintf("'%s' as a Go package name: %s", name, verdict.Note)}
	}
	clean := strings.ReplaceAll(strings.ToLower(name), "_", "")
	fix := "choose a lowercase name without underscores that is not predeclared"
	if clean != "" && registry.JudgeGoPackageName(clean).Status == registry.StatusAvailable {
		fix = "use the clean spelling instead: --to " + clean
	}
	return []string{fmt.Sprintf("'%s' as a Go package name is %s; %s", name, verdict.Note, fix)}
}

// CompanionTags is the tag the module proxy resolves the member's module
// at: path/vX.Y.Z below the repository root, and vX.Y.Z for the root module.
// Tag formats are declared, so a releasable's own tag is not always the
// proxy's; a companion equal to the release's own tag is dropped by
// ExpectedRefs.
func (goTarget) CompanionTags(memberPath string, version semver.Version) []string {
	if memberPath == "" || memberPath == "." {
		return []string{"v" + version.String()}
	}
	return []string{strings.TrimSuffix(memberPath, "/") + "/v" + version.String()}
}
