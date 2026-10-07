package runstate

import (
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// DeclassifyResultPath is the state of a `transition declassify` in
// progress: what its squashes rewrote and which of its steps completed, so
// a declassification that stops is finished by running it again. Its
// format belongs to the command.
const DeclassifyResultPath = declarations.ReleaseStateDir + "/declassify-result.json"

// LoadDeclassifyResult reads the declassify result. found is false when no
// declassification is in progress.
func LoadDeclassifyResult(root string) (data []byte, found bool, err error) {
	return readFile(root, DeclassifyResultPath)
}

// SaveDeclassifyResult replaces the declassify result with data,
// atomically.
func SaveDeclassifyResult(e *strictcli.Effects, root string, data []byte) error {
	return writeFile(e, root, DeclassifyResultPath, data)
}

// ClearDeclassifyResult removes the declassify result when it exists.
func ClearDeclassifyResult(e *strictcli.Effects, root string) error {
	return removeFile(e, root, DeclassifyResultPath)
}
