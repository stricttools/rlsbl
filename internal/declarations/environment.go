package declarations

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// LoadEnvironmentFile reads the environment file a declaration names: one
// KEY=VALUE pair per line, blank lines and lines starting with # ignored, a
// value's surrounding pair of matching quotes removed, and a leading ~/
// meaning the home directory. A missing file is an error, never an empty
// environment: a release built from it would run without the variables the
// declarations promise. So is a line that is none of the above.
func LoadEnvironmentFile(path string) (map[string]string, error) {
	expanded := path
	if rest, ok := strings.CutPrefix(path, "~/"); ok {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, fmt.Errorf("resolving environment_file %s: %w", path, err)
		}
		expanded = filepath.Join(home, rest)
	}
	data, err := os.ReadFile(expanded)
	if errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("environment_file %s (%s) does not exist; the release's environment is built from it, so create it, correct environment_file in %s, or delete the key", path, expanded, ReleasablesFile)
	}
	if err != nil {
		return nil, fmt.Errorf("reading environment_file %s: %w", expanded, err)
	}
	return ParseEnvironment(string(data), expanded)
}

// ParseEnvironment reads KEY=VALUE lines; name labels the text in errors.
func ParseEnvironment(text, name string) (map[string]string, error) {
	env := map[string]string{}
	for i, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		key = strings.TrimSpace(key)
		if !found || !envAssignment(key+"=") {
			return nil, fmt.Errorf("%s:%d: %q is not a KEY=VALUE line, a comment, or blank", name, i+1, line)
		}
		value = strings.TrimSpace(value)
		if len(value) >= 2 && (value[0] == '"' || value[0] == '\'') && value[len(value)-1] == value[0] {
			value = value[1 : len(value)-1]
		}
		env[key] = value
	}
	return env, nil
}
