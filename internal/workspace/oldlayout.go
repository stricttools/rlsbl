package workspace

import (
	"os"
	"path/filepath"
	"strings"
)

// OldLayoutDirectory names the old-layout directory a repository without
// release declarations holds, and is empty when it holds none: one at the
// root (.rlsbl/ or .rlsbl-monorepo/), or a .rlsbl/ anywhere tracked (the
// repository's tracked files) puts one. A repository holding one is managed
// by rlsbl and waits for its record migration.
func OldLayoutDirectory(root string, tracked []string) (string, error) {
	for _, dir := range []string{oldWorkspaceDir, oldStateDir} {
		if _, err := os.Lstat(filepath.Join(root, dir)); err == nil {
			return dir, nil
		} else if !os.IsNotExist(err) {
			return "", err
		}
	}
	for _, f := range tracked {
		parts := strings.Split(cleanPath(f), "/")
		for i, part := range parts[:len(parts)-1] {
			if part == oldStateDir || (i == 0 && part == oldWorkspaceDir) {
				return strings.Join(parts[:i+1], "/"), nil
			}
		}
	}
	return "", nil
}
