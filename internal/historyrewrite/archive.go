package historyrewrite

import (
	"path"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The ownership manifests of the directories the history rewrites write
// records into. A repository's first rewrite creates them, and its commit
// carries them.
var (
	rewritesManifest    = declarations.HistoryRewritesDir + "/manifest.toml"
	transitionsManifest = path.Dir(declarations.TransitionsFile) + "/manifest.toml"
)
