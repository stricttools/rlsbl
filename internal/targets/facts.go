package targets

// The closed vocabularies some facts take.
const (
	// MaterializeAlways: recreating a released version's missing ref is a
	// pure repair.
	MaterializeAlways = "materialize"
	// MaterializeUnlessIdentityChanged: the target's tags are its published
	// artifact (a Go tag is what the module proxy resolves and caches
	// forever), so a version released under another identity must not have
	// its refs recreated under the current one.
	MaterializeUnlessIdentityChanged = "refuse-identity-transition"

	// RenameManifestField: the package name is one field of the manifest,
	// which rlsbl rewrites.
	RenameManifestField = "manifest-field"
	// RenameGoModulePath: the package name is the module path's last
	// element, renamed by the module-path rewrite.
	RenameGoModulePath = "go-module-path"

	// ScratchGoNestedModule: a go.mod in each scratch directory keeps the go
	// command out of it.
	ScratchGoNestedModule = "go-nested-module"
	// ScratchPytestNorecursedirs: pytest's norecursedirs names the scratch
	// directories.
	ScratchPytestNorecursedirs = "pytest-norecursedirs"
	// ScratchRunnerChosenByProject: the project's manifest names the test
	// runner, whose configuration file rlsbl does not own and does not
	// write.
	ScratchRunnerChosenByProject = "runner-chosen-by-project"
)

// Facts are what a target is, declared once per target. The support matrix
// is these facts, and Axes says what each one means.
type Facts struct {
	Name string `json:"name"`
	// Ecosystem names the registry or platform for people.
	Ecosystem string `json:"ecosystem"`
	// DetectionFiles are the manifests whose presence in a directory
	// declares the target.
	DetectionFiles []string `json:"detection_files"`
	// ContentBasedDetection is set when detection also reads the manifest.
	ContentBasedDetection bool `json:"content_based_detection"`
	// VersionFiles are the files a version write changes.
	VersionFiles []string `json:"version_files"`
	// CompanionTag is the pattern of the extra tag a member owes, or empty.
	CompanionTag string `json:"companion_tag"`
	// RegistryDisplayName spells the registry in output.
	RegistryDisplayName string `json:"registry_display_name"`
	// BuildTimeoutSeconds bounds the release's build of the target; 0 when
	// the release builds nothing for it.
	BuildTimeoutSeconds int `json:"build_timeout_seconds"`
	// ProjectInitHint is what creates a project of this target.
	ProjectInitHint string `json:"project_init_hint"`
	// PublisherBindsToRepository is set when publishing is authorized for a
	// repository rather than by a token the project carries, so moving the
	// code needs a new authorization.
	PublisherBindsToRepository bool `json:"publisher_binds_to_repository"`
	// PublisherSetupURL is where a repository-bound publisher is registered.
	PublisherSetupURL string `json:"publisher_setup_url"`
	// ReleaseMaterializationPolicy is MaterializeAlways or
	// MaterializeUnlessIdentityChanged.
	ReleaseMaterializationPolicy string `json:"release_materialization_policy"`
	// PackageRename is RenameManifestField or RenameGoModulePath.
	PackageRename string `json:"package_rename"`
	// PackageNameField is where the package name is declared.
	PackageNameField string `json:"package_name_field"`
	// BuiltinTestCommand is what the built-in test runner runs.
	BuiltinTestCommand string `json:"builtin_test_command"`
	// TestSettings are the [members].test keys the target reads.
	TestSettings []string `json:"test_settings"`
	// DevInstallGlobal and DevInstallVenv are `rlsbl dev install`'s commands
	// per mode, or empty when the target has none for it.
	DevInstallGlobal string `json:"dev_install_global"`
	DevInstallVenv   string `json:"dev_install_venv"`
	// SharesWorkspaceEnvironment is set when a workspace's members of this
	// target resolve into one environment.
	SharesWorkspaceEnvironment bool `json:"shares_workspace_environment"`
	// PublishWorkflowAttests is set when the CI publish step attaches
	// attestations to a public transparency log unless told not to.
	PublishWorkflowAttests bool `json:"publish_workflow_attests"`
	// SupportsDepFloors is set when the manifest states dependency floors a
	// lockfile can resolve ahead of.
	SupportsDepFloors bool `json:"supports_dep_floors"`
	// ListsUploadOffline is set when the files an upload carries can be
	// listed without the network.
	ListsUploadOffline bool `json:"lists_upload_offline"`
	// ScratchTestExclusion is how the target's own test runner is kept out
	// of the scratch directories.
	ScratchTestExclusion string `json:"scratch_test_exclusion"`
}

// Axis is one fact of Facts, named by its JSON key.
type Axis struct {
	Name string `json:"name"`
	Doc  string `json:"doc"`
}

// Axes are the facts after the name, in Facts' order, each with what it
// says about a target.
var Axes = []Axis{
	{"ecosystem", "The registry or platform, named for people."},
	{"detection_files", "The manifests whose presence in a directory declares the target."},
	{"content_based_detection", "Whether detection also reads the manifest's content."},
	{"version_files", "The files a version write changes."},
	{"companion_tag", "The extra tag a member of this target owes a release besides the releasable's own tag, or empty."},
	{"registry_display_name", "How the registry is spelled in output."},
	{"build_timeout_seconds", "Seconds the release's build of the target may take; 0 when the release builds nothing for it."},
	{"project_init_hint", "What creates a project of this target."},
	{"publisher_binds_to_repository", "Whether publishing is authorized for a repository rather than by a token, so moving the code needs a new authorization."},
	{"publisher_setup_url", "Where a repository-bound publisher is registered; empty when none is."},
	{"release_materialization_policy", "Whether a reconcile may recreate a released version's missing refs unconditionally, or must refuse when the version was released under another identity."},
	{"package_rename", "How rlsbl rewrite project-name renames the package: manifest-field or go-module-path."},
	{"package_name_field", "Where the package name is declared."},
	{"builtin_test_command", "What the built-in test runner runs."},
	{"test_settings", "The keys of a member's test table the target reads."},
	{"dev_install_global", "What rlsbl dev install runs to install onto the machine; empty when nothing."},
	{"dev_install_venv", "What rlsbl dev install runs to install into the project's environment; empty when nothing."},
	{"shares_workspace_environment", "Whether a workspace's members of this target resolve into one environment."},
	{"publish_workflow_attests", "Whether the CI publish step attaches attestations to a public transparency log unless told not to."},
	{"supports_dep_floors", "Whether the manifest states dependency floors a lockfile can resolve ahead of."},
	{"lists_upload_offline", "Whether the files an upload carries are listed without the network, so private paths are refused before a release starts."},
	{"scratch_test_exclusion", "How the target's own test runner is kept out of the scratch directories."},
}
