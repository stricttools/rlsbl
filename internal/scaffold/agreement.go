package scaffold

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/targets"
)

// refuseNewVersionDisagreement refuses a scaffold that would create VERSION
// disagreeing with another target's version. A go target's VERSION is
// created at 0.0.0 when it does not exist, while npm and pypi declare their
// version in their own manifests; a project carrying two versions is
// refused, before anything is written, naming each target and its version.
// An existing VERSION is not compared here (version-consistency compares
// it).
func (c *memberContext) refuseNewVersionDisagreement() error {
	writers := map[string]bool{}
	versions := map[string]string{}
	for _, t := range c.targets {
		if t.name != declarations.TargetGo {
			continue
		}
		_, err := os.Stat(filepath.Join(t.abs, targets.VersionFile))
		switch {
		case errors.Is(err, fs.ErrNotExist):
			writers[t.name] = true
			versions[t.name] = "0.0.0"
		case err != nil:
			return err
		}
	}
	if len(writers) == 0 {
		return nil
	}
	for _, t := range c.targets {
		if t.name == declarations.TargetGo {
			continue
		}
		target, err := targets.Get(t.name)
		if err != nil {
			return err
		}
		v, err := target.ReadVersion(t.abs)
		if err != nil {
			return err
		}
		versions[t.name] = v.String()
	}
	distinct := map[string]bool{}
	for _, v := range versions {
		distinct[v] = true
	}
	if len(distinct) <= 1 {
		return nil
	}
	names := make([]string, 0, len(versions))
	for name := range versions {
		names = append(names, name)
	}
	// Manifest-declared versions first, then the VERSION writers, so the
	// listing does not depend on the order the targets are declared in.
	sort.Slice(names, func(i, j int) bool {
		if writers[names[i]] != writers[names[j]] {
			return !writers[names[i]]
		}
		return names[i] < names[j]
	})
	lines := make([]string, len(names))
	for i, name := range names {
		suffix := ""
		if writers[name] {
			suffix = " (the value VERSION would be created with)"
		}
		lines[i] = fmt.Sprintf("  %s: %s%s", name, versions[name], suffix)
	}
	return errors.New("Scaffolding would create VERSION, but the scaffolded targets disagree on the project's version:\n" +
		strings.Join(lines, "\n") +
		"\nA project carries one version. Create VERSION holding the project's version, then re-run `rlsbl scaffold`. Nothing was written.")
}
