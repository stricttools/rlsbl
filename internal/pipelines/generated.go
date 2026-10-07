package pipelines

import (
	"bytes"
	"encoding/json"
)

// The committed file generated from this package, relative to the
// repository root, and the command that regenerates it from there. A
// freshness test compares it with a fresh rendering.
const (
	TypeTablePath     = "internal/pipelines/pipeline-types.json"
	RegenerateCommand = "go run ./internal/pipelines/gen"
)

// typeTableFormatVersion is the shape version of the rendering: a reader of
// an older shape fails loudly instead of reading a renamed key as absent.
const typeTableFormatVersion = 1

// typeTableDocument is the committed rendering of TypeTable, which the docs
// directive table-pipelines reads.
type typeTableDocument struct {
	FormatVersion  int        `json:"format_version"`
	Generator      string     `json:"generator"`
	RegenerateWith string     `json:"regenerate_with"`
	Headers        []string   `json:"headers"`
	Rows           [][]string `json:"rows"`
}

// RenderTypeTable is the committed rendering of TypeTable.
func RenderTypeTable() ([]byte, error) {
	headers, rows := TypeTable()
	doc := typeTableDocument{FormatVersion: typeTableFormatVersion, Generator: "internal/pipelines/gen", RegenerateWith: RegenerateCommand, Headers: headers, Rows: rows}
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(doc); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}
