package registry

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// claimTimeout bounds each step of a name claim.
const claimTimeout = 2 * time.Minute

// Claimer performs a claim's effects: the strictcli effects handle.
type Claimer interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
	Write(path interface{}, content interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
	Mkdir(path interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
	Remove(path interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
}

// Credentials are where a claim authenticates from. Token is the secret
// itself where rlsbl must hand it over (PyPI); it is never printed.
type Credentials struct {
	// Source names where the credential comes from: an environment variable
	// or the registry's own login file.
	Source string
	Token  string
}

// Environment reads an environment variable; os.LookupEnv in the binary.
type Environment func(name string) (string, bool)

// ClaimCredentials finds the credentials a claim on eco authenticates with,
// or refuses naming every place it looked. npm: NPM_TOKEN when set,
// otherwise npm's own login in ~/.npmrc. PyPI: UV_PUBLISH_TOKEN, then
// PYPI_TOKEN, when set, otherwise the password of the [pypi] section of
// ~/.pypirc.
func ClaimCredentials(eco Ecosystem, env Environment, home string) (Credentials, error) {
	switch eco {
	case Npm:
		if v, ok := env("NPM_TOKEN"); ok && v != "" {
			return Credentials{Source: "NPM_TOKEN"}, nil
		}
		has, err := npmrcHasLogin(filepath.Join(home, ".npmrc"))
		if err != nil {
			return Credentials{}, err
		}
		if has {
			return Credentials{Source: "~/.npmrc"}, nil
		}
		return Credentials{}, errors.New("no npm credentials to claim a name with: NPM_TOKEN is not set, and ~/.npmrc holds no registry login (the _authToken line `npm login` writes). Log in with `npm login`, or set NPM_TOKEN")
	case Pypi:
		for _, name := range []string{"UV_PUBLISH_TOKEN", "PYPI_TOKEN"} {
			if v, ok := env(name); ok && v != "" {
				return Credentials{Source: name, Token: v}, nil
			}
		}
		token, err := pypircToken(filepath.Join(home, ".pypirc"))
		if err != nil {
			return Credentials{}, err
		}
		if token != "" {
			return Credentials{Source: "~/.pypirc", Token: token}, nil
		}
		return Credentials{}, errors.New("no PyPI credentials to claim a name with: neither UV_PUBLISH_TOKEN nor PYPI_TOKEN is set, and ~/.pypirc has no password in its [pypi] section. Put an API token there (username = __token__, password = pypi-...), or set UV_PUBLISH_TOKEN")
	}
	return Credentials{}, fmt.Errorf("a name cannot be claimed on %q (claim-name publishes to npm and pypi)", eco)
}

// npmrcHasLogin reports whether an npm config file carries a registry
// login: an _authToken or _auth key with a value. A missing file has none.
func npmrcHasLogin(path string) (bool, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("~/.npmrc cannot be read: %w", err)
	}
	for _, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || line[0] == '#' || line[0] == ';' {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		key = strings.TrimSpace(key)
		if ok && strings.TrimSpace(value) != "" && (strings.HasSuffix(key, "_authToken") || strings.HasSuffix(key, "_auth")) {
			return true, nil
		}
	}
	return false, nil
}

// pypircToken is the password of the [pypi] section of the pypirc at path,
// or "" when the file or the password is absent. A file that is not an INI
// file is refused.
func pypircToken(path string) (string, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("~/.pypirc cannot be read: %w", err)
	}
	section := ""
	sc := bufio.NewScanner(bytes.NewReader(data))
	for n := 1; sc.Scan(); n++ {
		line := strings.TrimSpace(sc.Text())
		switch {
		case line == "" || line[0] == '#' || line[0] == ';':
			continue
		case strings.HasPrefix(line, "["):
			if !strings.HasSuffix(line, "]") {
				return "", fmt.Errorf("~/.pypirc cannot be read as an INI file (line %d opens a section it does not close); fix it, or set UV_PUBLISH_TOKEN", n)
			}
			section = strings.TrimSpace(line[1 : len(line)-1])
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			key, value, ok = strings.Cut(line, ":")
		}
		if !ok {
			return "", fmt.Errorf("~/.pypirc cannot be read as an INI file (line %d is neither a section nor a key); fix it, or set UV_PUBLISH_TOKEN", n)
		}
		if section == "pypi" && strings.EqualFold(strings.TrimSpace(key), "password") {
			return strings.TrimSpace(value), nil
		}
	}
	return "", sc.Err()
}

// ClaimURL is where a claimed name is shown on its registry.
func ClaimURL(eco Ecosystem, name string) string {
	if eco == Npm {
		return "https://www.npmjs.com/package/" + name
	}
	return "https://pypi.org/project/" + name + "/"
}

// ClaimPlaceholder publishes a version 0.0.0 placeholder of name to eco from
// a scratch directory under the system's temporary directory, which it
// removes afterwards. grant is the name of the command's grant for the
// publish. Under --dry-run every write and the publish are recorded, not
// performed. The token never reaches an argument: npm reads ${NPM_TOKEN}
// itself, and uv receives the PyPI token in its environment.
func ClaimPlaceholder(e Claimer, eco Ecosystem, name string, creds Credentials, grant string) (err error) {
	suffix := make([]byte, 6)
	if _, err := rand.Read(suffix); err != nil {
		return err
	}
	dir := filepath.Join(os.TempDir(), "rlsbl-claim-"+hex.EncodeToString(suffix))
	if _, err := e.Mkdir(dir); err != nil {
		return err
	}
	defer func() {
		if _, rmErr := e.Remove(dir); rmErr != nil && err == nil {
			err = rmErr
		}
	}()
	switch eco {
	case Npm:
		if creds.Source == "NPM_TOKEN" {
			// npm reads a token only from an .npmrc; this one names the
			// variable, which npm expands itself, so no secret is written.
			if _, err := e.Write(filepath.Join(dir, ".npmrc"), "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n"); err != nil {
				return err
			}
		}
		manifest, err := json.MarshalIndent(map[string]string{"name": name, "version": "0.0.0", "description": "Name reservation"}, "", "  ")
		if err != nil {
			return err
		}
		if _, err := e.Write(filepath.Join(dir, "package.json"), string(manifest)+"\n"); err != nil {
			return err
		}
		_, err = e.Run([]interface{}{"npm", "publish", "--access", "public"}, strictcli.Cwd(dir), strictcli.Stream(true),
			strictcli.Timeout(claimTimeout), strictcli.UseGrant(grant), strictcli.Resource("npm:"+name))
		return err
	case Pypi:
		if creds.Token == "" {
			return errors.New("a PyPI claim needs a token to hand to uv")
		}
		pkg := filepath.Join(dir, strings.ReplaceAll(name, "-", "_"))
		if _, err := e.Mkdir(pkg); err != nil {
			return err
		}
		if _, err := e.Write(filepath.Join(pkg, "__init__.py"), ""); err != nil {
			return err
		}
		pyproject := fmt.Sprintf("[project]\nname = %q\nversion = \"0.0.0\"\ndescription = \"Name reservation\"\nrequires-python = \">=3.11\"\n\n[build-system]\nrequires = [\"hatchling\"]\nbuild-backend = \"hatchling.build\"\n", name)
		if _, err := e.Write(filepath.Join(dir, "pyproject.toml"), pyproject); err != nil {
			return err
		}
		if _, err := e.Run([]interface{}{"uv", "build"}, strictcli.Cwd(dir), strictcli.Stream(true), strictcli.Timeout(claimTimeout)); err != nil {
			return err
		}
		_, err := e.Run([]interface{}{"uv", "publish"}, strictcli.Cwd(dir), strictcli.Stream(true), strictcli.Timeout(claimTimeout),
			strictcli.EffectEnv(map[string]string{"UV_PUBLISH_TOKEN": creds.Token}), strictcli.Redact(creds.Token),
			strictcli.UseGrant(grant), strictcli.Resource("pypi:"+name))
		return err
	}
	return fmt.Errorf("a name cannot be claimed on %q (claim-name publishes to npm and pypi)", eco)
}
