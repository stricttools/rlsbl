package pipelines

import (
	"fmt"
	"os/exec"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
)

// The bounds of a local publish's steps: `go install` may compile a module
// and its dependencies from scratch, which takes longer than a registry
// call.
const (
	publishTimeout = 5 * time.Minute
	installTimeout = 5 * time.Minute
)

// Runner starts programs: the strictcli effects handle.
type Runner interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
}

// LocalPublish is one local publish of a pipeline.
type LocalPublish struct {
	Pipeline declarations.Pipeline
	// Dir is the directory of the target the pipeline publishes, absolute.
	Dir     string
	Version semver.Version
	// Grant is the publishing command's grant for the registry write, or
	// empty when it declares none.
	Grant string
}

// Published is what a local publish did.
type Published struct {
	// Skipped is set when the registry already lists the version, so
	// nothing was published.
	Skipped bool
	// Message says what happened.
	Message string
}

// PublishLocal publishes the pipeline from this machine. A pipeline
// publishing from CI is refused (its publish workflow publishes), and so is
// a go-binary pipeline, whose platform packages are built from release
// archives only CI has. npm and PyPI are first asked, through the package
// listing (never one version's metadata), whether the version is already
// published; a listed version is skipped. Go's tags were pushed before this
// step, so the proxy is notified and the declared main packages installed
// whatever the tag's state: the notification and the install both repeat
// harmlessly.
func PublishLocal(r Runner, reg registry.Client, env registry.Environment, in LocalPublish) (Published, error) {
	p := in.Pipeline
	if !p.Local {
		return Published{}, fmt.Errorf("the pipeline %q publishes from CI (local = false); its publish workflow publishes it", p.Name)
	}
	if p.Artifact == declarations.ArtifactGoBinary {
		return Published{}, fmt.Errorf("the pipeline %q publishes go binaries, whose platform packages are built from the release archives CI builds; declare it local = false", p.Name)
	}
	switch p.Type {
	case declarations.TargetGo:
		return publishGo(r, in)
	case declarations.TargetNPM, declarations.TargetPyPI:
		return publishPackage(r, reg, env, in)
	}
	_, err := TypeOf(p.Type)
	return Published{}, err
}

// lookPath refuses a program that is not on PATH, saying what it is for.
func lookPath(program, purpose string) error {
	if _, err := exec.LookPath(program); err != nil {
		return fmt.Errorf("%s is not on PATH, and it is needed %s", program, purpose)
	}
	return nil
}

func run(r Runner, argv []string, opts ...strictcli.EffectOption) error {
	args := make([]interface{}, len(argv))
	for i, a := range argv {
		args[i] = a
	}
	_, err := r.Run(args, opts...)
	return err
}

// publishGo notifies the Go module proxy of the version and installs the
// pipeline's declared main packages.
func publishGo(r Runner, in LocalPublish) (Published, error) {
	if err := lookPath("go", "to notify the Go module proxy and install the binaries"); err != nil {
		return Published{}, err
	}
	module, found, err := gomodule.ModulePath(in.Dir)
	if err != nil {
		return Published{}, err
	}
	if !found {
		return Published{}, fmt.Errorf("%s holds no go.mod, so the pipeline %q has no module to publish", in.Dir, in.Pipeline.Name)
	}
	if in.Pipeline.InstallPaths == nil {
		return Published{}, fmt.Errorf("the local go pipeline %q declares no install_paths; declare the main packages it installs in .strictmetadata/releasables/releasables.toml", in.Pipeline.Name)
	}
	// The install paths are validated before the notification: the
	// validation observes the module, and under --dry-run an observe after
	// a recorded effect has no answer.
	paths, err := gomodule.ValidateInstallPaths(r, in.Dir, in.Pipeline.InstallPaths)
	if err != nil {
		return Published{}, err
	}
	ref := module + "@v" + in.Version.String()
	// The argv is not the observe allowlist's `go list -m`, which a preview
	// runs rather than records: under --dry-run the tag was never pushed, and asking
	// the proxy about an unpublished version of the module can burn it. A
	// preview records this notification instead.
	if err := run(r, []string{"go", "list", "-json", "-m", ref}, strictcli.Cwd(in.Dir), strictcli.EffectEnv(map[string]string{"GOPROXY": "proxy.golang.org"}),
		strictcli.Stream(true), strictcli.Timeout(publishTimeout)); err != nil {
		return Published{}, fmt.Errorf("notifying the Go module proxy of %s: %w", ref, err)
	}
	for _, p := range paths {
		if err := run(r, []string{"go", "install", p}, strictcli.Cwd(in.Dir), strictcli.Stream(true), strictcli.Timeout(installTimeout)); err != nil {
			return Published{}, fmt.Errorf("go install %s: %w", p, err)
		}
	}
	return Published{Message: fmt.Sprintf("notified the Go module proxy of %s and installed %d main package(s)", ref, len(paths))}, nil
}

// publishPackage publishes an npm or PyPI package unless the registry's
// package listing already holds the version.
func publishPackage(r Runner, reg registry.Client, env registry.Environment, in LocalPublish) (Published, error) {
	p := in.Pipeline
	target, err := targets.Get(p.Type)
	if err != nil {
		return Published{}, err
	}
	name, found, err := target.ReadName(in.Dir)
	if err != nil {
		return Published{}, err
	}
	if !found {
		return Published{}, fmt.Errorf("the manifest in %s names no package, so the pipeline %q has nothing to publish", in.Dir, p.Name)
	}
	version := in.Version.String()
	var listed bool
	switch p.Type {
	case declarations.TargetNPM:
		pkg, exists, err := reg.NpmPackage(name)
		if err != nil {
			return Published{}, err
		}
		listed = exists && pkg.Has(version)
	default:
		project, exists, err := reg.PypiProject(name)
		if err != nil {
			return Published{}, err
		}
		listed = exists && project.Has(version)
	}
	where := target.Facts().RegistryDisplayName
	if listed {
		return Published{Skipped: true, Message: fmt.Sprintf("%s already lists %s %s, so nothing was published", where, name, version)}, nil
	}
	secrets, err := LocalSecretNames(p)
	if err != nil {
		return Published{}, err
	}
	token, ok := env(secrets[0])
	if !ok || token == "" {
		return Published{}, fmt.Errorf("the local pipeline %q publishes with %s, which is not set", p.Name, secrets[0])
	}
	// The registry write carries the command's grant when it declares one.
	write := []strictcli.EffectOption{strictcli.Resource(p.Type + ":" + name)}
	if in.Grant != "" {
		write = append(write, strictcli.UseGrant(in.Grant))
	}
	if p.Type == declarations.TargetNPM {
		if err := lookPath("npm", "to publish to npm"); err != nil {
			return Published{}, err
		}
		// npm reads the token through the project's .npmrc, which names
		// ${NPM_TOKEN}; the token never reaches an argument.
		err = run(r, []string{"npm", "publish", "--access", "public"}, append([]strictcli.EffectOption{strictcli.Cwd(in.Dir), strictcli.EffectEnv(map[string]string{"NPM_TOKEN": token}),
			strictcli.Redact(token), strictcli.Stream(true), strictcli.Timeout(publishTimeout)}, write...)...)
	} else {
		if err := lookPath("uv", "to build and publish to PyPI"); err != nil {
			return Published{}, err
		}
		if err := run(r, []string{"uv", "build"}, strictcli.Cwd(in.Dir), strictcli.Stream(true), strictcli.Timeout(publishTimeout)); err != nil {
			return Published{}, err
		}
		err = run(r, []string{"uv", "publish", "--check-url", "https://pypi.org/simple/"}, append([]strictcli.EffectOption{strictcli.Cwd(in.Dir), strictcli.EffectEnv(map[string]string{"UV_PUBLISH_TOKEN": token}),
			strictcli.Redact(token), strictcli.Stream(true), strictcli.Timeout(publishTimeout)}, write...)...)
	}
	if err != nil {
		return Published{}, fmt.Errorf("publishing %s %s to %s: %w", name, version, where, err)
	}
	return Published{Message: fmt.Sprintf("published %s %s to %s", name, version, where)}, nil
}
