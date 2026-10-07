// Package pipelines is what rlsbl's publish pipelines mean: the npm, pypi,
// and go pipeline types, the artifacts each publishes, the repository
// secrets a CI publish job reads, whether a pipeline asks the Go module
// proxy about the repository's module, a local publish from this machine,
// and the platform table one go binary pipeline's binaries are packaged for
// (npm platform packages and PyPI binary wheels alike).
package pipelines

import (
	"fmt"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// Type is one pipeline type: what it publishes to and how it authenticates.
type Type struct {
	// Name is go, npm, or pypi: the pipeline types are the targets they
	// publish.
	Name string
	// Destination names where it publishes, for people.
	Destination string
	// Auth is how a publish from CI authenticates, for people; the secrets
	// it reads are CISecrets.
	Auth string
	// CISecrets are the repository secrets the CI publish job reads. The
	// names are written into the publish workflow templates, so the workflow
	// and this list cannot disagree; GITHUB_TOKEN, which Actions supplies to
	// every job, is never listed.
	CISecrets []string
	// LocalSecrets are the environment variables a local publish reads.
	LocalSecrets []string
	// Artifacts are the artifact values a pipeline of this type takes.
	Artifacts []string
}

// types are the pipeline types, in name order.
var types = []Type{
	{
		Name:        declarations.TargetGo,
		Destination: "the Go module proxy (a module is published by its tag), and GitHub Release archives for a binary",
		Auth:        "nothing",
		Artifacts:   []string{declarations.ArtifactBinary, declarations.ArtifactLibrary},
	},
	{
		Name:         declarations.TargetNPM,
		Destination:  "the npm registry",
		Auth:         "a token",
		CISecrets:    []string{"NPM_TOKEN"},
		LocalSecrets: []string{"NPM_TOKEN"},
		Artifacts:    []string{declarations.ArtifactPackage, declarations.ArtifactGoBinary},
	},
	{
		Name:         declarations.TargetPyPI,
		Destination:  "the Python Package Index",
		Auth:         "trusted publishing (OIDC), no secret",
		LocalSecrets: []string{"PYPI_TOKEN"},
		Artifacts:    []string{declarations.ArtifactPackage, declarations.ArtifactGoBinary},
	},
}

// Types are the pipeline types, in name order.
func Types() []Type {
	out := make([]Type, len(types))
	copy(out, types)
	return out
}

// TypeOf is the pipeline type named name; any other name is refused,
// naming the types there are.
func TypeOf(name string) (Type, error) {
	var names []string
	for _, t := range types {
		if t.Name == name {
			return t, nil
		}
		names = append(names, t.Name)
	}
	return Type{}, fmt.Errorf("%q is not a pipeline type; the pipeline types are %s", name, strings.Join(names, ", "))
}

// CISecretNames are the repository secrets the pipeline's CI publish job
// authenticates with: none for a pipeline publishing locally, whose
// credential is this machine's environment.
func CISecretNames(p declarations.Pipeline) ([]string, error) {
	t, err := TypeOf(p.Type)
	if err != nil {
		return nil, err
	}
	if p.Local {
		return nil, nil
	}
	return append([]string(nil), t.CISecrets...), nil
}

// LocalSecretNames are the environment variables the pipeline's local
// publish reads: none for a pipeline publishing from CI.
func LocalSecretNames(p declarations.Pipeline) ([]string, error) {
	t, err := TypeOf(p.Type)
	if err != nil {
		return nil, err
	}
	if !p.Local {
		return nil, nil
	}
	return append([]string(nil), t.LocalSecrets...), nil
}

// AsksGoProxy reports whether the pipeline asks the Go module proxy about
// the repository's module, which records the module (and so the
// repository) publicly and permanently: a local go pipeline notifies the
// proxy from this machine, and a go library published from CI asks it for
// the new version. A go binary published from CI never asks it.
func AsksGoProxy(p declarations.Pipeline) bool {
	return p.Type == declarations.TargetGo && (p.Local || p.Artifact == declarations.ArtifactLibrary)
}

// TypeTable is the pipeline types as the docs render them, from the
// committed rendering TypeTablePath holds.
func TypeTable() (headers []string, rows [][]string) {
	headers = []string{"Type", "Publishes to", "Artifacts", "Authenticates in CI", "Authenticates locally"}
	for _, t := range types {
		ci := t.Auth
		if len(t.CISecrets) > 0 {
			ci += ": the " + codeList(t.CISecrets) + " Actions secret"
		}
		local := "nothing"
		if len(t.LocalSecrets) > 0 {
			local = codeList(t.LocalSecrets)
		}
		rows = append(rows, []string{"`" + t.Name + "`", t.Destination, codeList(t.Artifacts), ci, local})
	}
	return headers, rows
}

// codeList is names as code spans, comma-separated.
func codeList(names []string) string {
	quoted := make([]string, len(names))
	for i, n := range names {
		quoted[i] = "`" + n + "`"
	}
	return strings.Join(quoted, ", ")
}
