package declarations

import "github.com/stricttools/strictcli/go/strictcli"

// EnsureOwnedDirectory creates dir (repository-relative, directly under
// .strictmetadata/) and its manifest.toml naming rlsbl when the manifest is
// missing, and refuses a manifest naming another owner. Every writer of a
// record rlsbl owns calls it before writing into the directory.
func EnsureOwnedDirectory(e *strictcli.Effects, root, dir string) error {
	return ensureManifest(e, root, dir)
}
