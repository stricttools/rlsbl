package registry

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"
)

// envOf is an Environment holding exactly vars.
func envOf(vars map[string]string) Environment {
	return func(name string) (string, bool) {
		v, ok := vars[name]
		return v, ok
	}
}

// homeWith is a home directory holding the named files.
func homeWith(t *testing.T, files map[string]string) string {
	t.Helper()
	home := t.TempDir()
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(home, name), []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return home
}

func TestNpmClaimCredentials(t *testing.T) {
	hygiene.Isolate(t)
	if c, err := ClaimCredentials(Npm, envOf(map[string]string{"NPM_TOKEN": "npm_x"}), homeWith(t, nil)); err != nil || c.Source != "NPM_TOKEN" || c.Token != "" {
		t.Errorf("an environment token: %+v %v", c, err)
	}
	login := homeWith(t, map[string]string{".npmrc": "# login\n//registry.npmjs.org/:_authToken=npm_abc\n"})
	if c, err := ClaimCredentials(Npm, envOf(nil), login); err != nil || c.Source != "~/.npmrc" {
		t.Errorf("npm's own login: %+v %v", c, err)
	}
	_, err := ClaimCredentials(Npm, envOf(map[string]string{"NPM_TOKEN": ""}), homeWith(t, map[string]string{".npmrc": "registry=https://example.invalid\n"}))
	if err == nil || !strings.Contains(err.Error(), "NPM_TOKEN") || !strings.Contains(err.Error(), "~/.npmrc") {
		t.Errorf("neither: %v", err)
	}
}

func TestPypiClaimCredentials(t *testing.T) {
	hygiene.Isolate(t)
	pypirc := homeWith(t, map[string]string{".pypirc": "[distutils]\nindex-servers = pypi\n\n[pypi]\nusername = __token__\npassword = pypi-fromfile\n"})
	c, err := ClaimCredentials(Pypi, envOf(map[string]string{"PYPI_TOKEN": "pypi-b", "UV_PUBLISH_TOKEN": "pypi-a"}), pypirc)
	if err != nil || c.Source != "UV_PUBLISH_TOKEN" || c.Token != "pypi-a" {
		t.Errorf("the environment wins: %+v %v", c.Source, err)
	}
	c, err = ClaimCredentials(Pypi, envOf(nil), pypirc)
	if err != nil || c.Source != "~/.pypirc" || c.Token != "pypi-fromfile" {
		t.Errorf("the pypirc: %+v %v", c.Source, err)
	}
	_, err = ClaimCredentials(Pypi, envOf(nil), homeWith(t, map[string]string{".pypirc": "[testpypi]\npassword = pypi-other\n"}))
	if err == nil || !strings.Contains(err.Error(), "UV_PUBLISH_TOKEN") || !strings.Contains(err.Error(), "~/.pypirc") || strings.Contains(err.Error(), "pypi-other") {
		t.Errorf("neither: %v", err)
	}
	if _, err := ClaimCredentials(Pypi, envOf(nil), homeWith(t, map[string]string{".pypirc": "[pypi\npassword = x\n"})); err == nil || !strings.Contains(err.Error(), "INI") {
		t.Errorf("a broken pypirc: %v", err)
	}
	if _, err := ClaimCredentials(Go, envOf(nil), homeWith(t, nil)); err == nil {
		t.Error("a claim on go was accepted")
	}
}

// recordingClaimer records a claim's effects instead of performing them.
type recordingClaimer struct {
	runs    [][]string
	writes  map[string]string
	removed []string
}

func (r *recordingClaimer) Run(argv []interface{}, _ ...strictcli.EffectOption) (strictcli.Completed, error) {
	var words []string
	for _, a := range argv {
		words = append(words, a.(string))
	}
	r.runs = append(r.runs, words)
	return strictcli.Completed{}, nil
}

func (r *recordingClaimer) Write(path interface{}, content interface{}, _ ...strictcli.EffectOption) (strictcli.Unsettled, error) {
	if r.writes == nil {
		r.writes = map[string]string{}
	}
	r.writes[filepath.Base(path.(string))] = content.(string)
	return strictcli.Unsettled{}, nil
}

func (r *recordingClaimer) Mkdir(interface{}, ...strictcli.EffectOption) (strictcli.Unsettled, error) {
	return strictcli.Unsettled{}, nil
}

func (r *recordingClaimer) Remove(path interface{}, _ ...strictcli.EffectOption) (strictcli.Unsettled, error) {
	r.removed = append(r.removed, path.(string))
	return strictcli.Unsettled{}, nil
}

// With NPM_TOKEN set, the placeholder's .npmrc names the variable and never
// holds the token; the scratch directory is removed afterwards.
func TestAnNpmClaimNeverWritesTheToken(t *testing.T) {
	hygiene.Isolate(t)
	rec := &recordingClaimer{}
	if err := ClaimPlaceholder(rec, Npm, "portal", Credentials{Source: "NPM_TOKEN"}, "publish"); err != nil {
		t.Fatal(err)
	}
	if rec.writes[".npmrc"] != "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n" {
		t.Errorf(".npmrc: %q", rec.writes[".npmrc"])
	}
	if !strings.Contains(rec.writes["package.json"], `"version": "0.0.0"`) || !strings.Contains(rec.writes["package.json"], `"name": "portal"`) {
		t.Errorf("package.json: %q", rec.writes["package.json"])
	}
	if len(rec.runs) != 1 || strings.Join(rec.runs[0], " ") != "npm publish --access public" || len(rec.removed) != 1 {
		t.Errorf("runs %v, removed %v", rec.runs, rec.removed)
	}
}

func TestAPypiClaimKeepsTheTokenOutOfEveryArgument(t *testing.T) {
	hygiene.Isolate(t)
	rec := &recordingClaimer{}
	if err := ClaimPlaceholder(rec, Pypi, "my-gadget", Credentials{Source: "~/.pypirc", Token: "pypi-secret"}, "publish"); err != nil {
		t.Fatal(err)
	}
	if len(rec.runs) != 2 || strings.Join(rec.runs[0], " ") != "uv build" || strings.Join(rec.runs[1], " ") != "uv publish" {
		t.Fatalf("runs %v", rec.runs)
	}
	for _, content := range rec.writes {
		if strings.Contains(content, "pypi-secret") {
			t.Fatal("the token was written to a file")
		}
	}
	if !strings.Contains(rec.writes["pyproject.toml"], `name = "my-gadget"`) {
		t.Errorf("pyproject.toml: %q", rec.writes["pyproject.toml"])
	}
	if _, ok := rec.writes["__init__.py"]; !ok {
		t.Error("no package module was written")
	}
}
