package dependencies_test

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/dependencies"
)

func overlayFile(t *testing.T, dir, name string, entries map[string]string) {
	t.Helper()
	text := ""
	for pkg, path := range entries {
		text += "[[overlay]]\npackage = \"" + pkg + "\"\npath = \"" + path + "\"\n\n"
	}
	write(t, dir, name, text)
}

func TestActiveOverlaysNeedBothFilesToAgree(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	checkout := mkdir(t, t.TempDir(), "strictcli")
	if got, err := dependencies.ActiveOverlays(dir); err != nil || got != nil {
		t.Fatalf("registry mode: %v, %v", got, err)
	}
	overlayFile(t, dir, dependencies.OverridesFile, map[string]string{"strictcli": checkout})
	if _, err := dependencies.ActiveOverlays(dir); err == nil || !strings.Contains(err.Error(), "never installed") || !strings.Contains(err.Error(), "rlsbl dev sync") {
		t.Fatalf("declared but not synced: %v", err)
	}
	overlayFile(t, dir, dependencies.SentinelFile, map[string]string{"strictcli": checkout})
	got, err := dependencies.ActiveOverlays(dir)
	if err != nil || len(got) != 1 || got[0].Path != resolvedDir(t, checkout) {
		t.Fatalf("%v, %v", got, err)
	}
	overlayFile(t, dir, dependencies.SentinelFile, map[string]string{"strictcli": mkdir(t, t.TempDir(), "other")})
	if _, err := dependencies.ActiveOverlays(dir); err == nil || !strings.Contains(err.Error(), "was synced from") {
		t.Fatalf("synced elsewhere: %v", err)
	}
	overlayFile(t, dir, dependencies.SentinelFile, map[string]string{"strictspec": checkout})
	if _, err := dependencies.ActiveOverlays(dir); err == nil || !strings.Contains(err.Error(), "records strictspec") {
		t.Fatalf("different packages: %v", err)
	}
}

func TestAnUnreadableSentinelIsAnErrorNotNoOverlays(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	if _, found, err := dependencies.LoadSentinel(dir); found || err != nil {
		t.Fatalf("%v, %v", found, err)
	}
	write(t, dir, dependencies.SentinelFile, "[[overlay\n")
	if _, _, err := dependencies.LoadSentinel(dir); err == nil || !strings.Contains(err.Error(), "regenerable local state") {
		t.Fatalf("%v", err)
	}
}

func TestCollectActiveOverlaysRefusesOnePackageFromTwoCheckouts(t *testing.T) {
	hygiene.Isolate(t)
	a, b := t.TempDir(), t.TempDir()
	first, second := mkdir(t, t.TempDir(), "kit"), mkdir(t, t.TempDir(), "kit")
	for dir, checkout := range map[string]string{a: first, b: second} {
		overlayFile(t, dir, dependencies.OverridesFile, map[string]string{"kit": checkout})
		overlayFile(t, dir, dependencies.SentinelFile, map[string]string{"kit": checkout})
	}
	if _, err := dependencies.CollectActiveOverlays([]string{a, b}); err == nil || !strings.Contains(err.Error(), "two different checkouts") {
		t.Fatalf("%v", err)
	}
	if got, err := dependencies.CollectActiveOverlays([]string{a, a}); err != nil || len(got) != 1 {
		t.Fatalf("%v, %v", got, err)
	}
}

// distInfo writes a dist-info directory into the environment's
// site-packages.
func distInfo(t *testing.T, env, name, version, directURL string) {
	t.Helper()
	info := filepath.Join(env, "lib", "python3.14", "site-packages", name+"-"+version+".dist-info")
	write(t, info, "METADATA", "Metadata-Version: 2.4\nName: "+name+"\nVersion: "+version+"\n\nThe description.\n")
	if directURL != "" {
		write(t, info, "direct_url.json", directURL)
	}
}

func TestInspectAndClassifyOverlays(t *testing.T) {
	hygiene.Isolate(t)
	root := workspaceRoot(t, `["packages/*"]`, "")
	member := mkdir(t, root, "packages/widget")
	checkout := mkdir(t, t.TempDir(), "strictcli")
	env, err := dependencies.ProjectEnvironment(member, "")
	if err != nil || env != filepath.Join(resolvedDir(t, root), ".venv") {
		t.Fatalf("a member shares the root's environment: %s, %v", env, err)
	}
	entry := dependencies.Overlay{Package: "strictcli", Path: checkout}

	installed, err := dependencies.InspectInstalled(member, "", "strictcli")
	if err != nil || installed.Found {
		t.Fatalf("%+v, %v", installed, err)
	}
	if state, line := dependencies.ClassifyOverlay(entry, installed); state != dependencies.OverlayMissing || !strings.Contains(line, env) {
		t.Errorf("%s: %s", state, line)
	}

	distInfo(t, env, "strictcli", "0.36.0", "")
	installed, _ = dependencies.InspectInstalled(member, "", "StrictCLI")
	if state, line := dependencies.ClassifyOverlay(entry, installed); state != dependencies.OverlayWiped || !strings.Contains(line, "registry install (0.36.0)") {
		t.Errorf("%s: %s", state, line)
	}

	distInfo(t, env, "strictcli", "0.36.0", `{"url": "file://`+checkout+`", "dir_info": {"editable": true}}`)
	installed, _ = dependencies.InspectInstalled(member, "", "strictcli")
	if state, _ := dependencies.ClassifyOverlay(entry, installed); state != dependencies.OverlayHealthy {
		t.Errorf("%s: %+v", state, installed)
	}
	elsewhere := dependencies.Overlay{Package: "strictcli", Path: t.TempDir()}
	if state, line := dependencies.ClassifyOverlay(elsewhere, installed); state != dependencies.OverlayWiped || !strings.Contains(line, "not the declared overlay path") {
		t.Errorf("%s: %s", state, line)
	}

	relocated, err := dependencies.ProjectEnvironment(member, "envs/dev")
	if err != nil || relocated != filepath.Join(resolvedDir(t, root), "envs", "dev") {
		t.Errorf("UV_PROJECT_ENVIRONMENT relative to the root: %s, %v", relocated, err)
	}
}
