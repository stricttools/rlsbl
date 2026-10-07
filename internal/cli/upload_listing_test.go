package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// fakeNpmPack puts first on PATH an npm whose `pack --dry-run --json` lists
// package.json and index.js after writing its cache and logs the way npm
// does: under npm_config_cache, which must be set and lie outside repo. It
// records each cache it was given in caches.txt beside it, and returns that
// file.
func fakeNpmPack(t *testing.T, repo string) string {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\n" +
		"case \"$npm_config_cache\" in \"\"|\"" + repo + "\"*) echo \"npm's cache is not a directory outside the repository: '$npm_config_cache'\" >&2; exit 1;; esac\n" +
		"mkdir -p \"$npm_config_cache/_logs\" && echo log > \"$npm_config_cache/_logs/debug.log\" || exit 1\n" +
		"printf '%s\\n' \"$npm_config_cache\" >> \"${0%/*}/caches.txt\"\n" +
		"echo '[{\"files\":[{\"path\":\"package.json\"},{\"path\":\"index.js\"}]}]'\n"
	if err := os.WriteFile(filepath.Join(dir, "npm"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return filepath.Join(dir, "caches.txt")
}

func TestAReadOnlyCheckRunListsAnNpmPackagesUpload(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\ngithub_repository = \"acme/portal\"\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"ci\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n")
	repo.Write("package.json", "{\n  \"name\": \"portal\",\n  \"version\": \"0.1.0\",\n  \"license\": \"MIT\"\n}\n")
	repo.Write("index.js", "module.exports = {}\n")
	repo.Commit("the package", ".strictmetadata/releasables/releasables.toml", "package.json", "index.js")
	hygiene.Chdir(t, repo.Dir)
	caches := fakeNpmPack(t, repo.Dir)

	text := mustCheckStatus(t, appWith(t, testsupport.NewFakeHTTP(t)), "upload-private-paths", "pass")
	data, err := os.ReadFile(caches)
	if err != nil {
		t.Fatalf("npm was never asked to list the package: %v\n%s", err, text)
	}
	for _, cache := range strings.Fields(string(data)) {
		if _, err := os.Lstat(cache); !os.IsNotExist(err) {
			t.Errorf("npm's cache %s was left behind: %v", cache, err)
		}
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the check run changed the repository:\n%s", status)
	}
}
