package monorepo

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/saferm"
	"github.com/stricttools/rlsbl/internal/workflows"
)

// InitCommitMessage is the message of the commit `monorepo init` makes.
const InitCommitMessage = "monorepo: init workspace"

// InitRequest is one `monorepo init`.
type InitRequest struct {
	// Root is the repository root, absolute.
	Root string
	// ReleaseBranches are the branches a release may run from.
	ReleaseBranches []string
	// RootReleasable is the releasable the root member is versioned under,
	// created with it; nil declares the root member a dev node.
	RootReleasable *declarations.Releasable
	// AutoCommit commits the declarations.
	AutoCommit bool
	// Say prints one line of the report.
	Say func(string)
}

// Init writes the declarations of a workspace whose only member is the root
// member: a dev node, or versioned under the releasable the request
// creates. A repository that already declares its layout is refused. When
// the commit fails, the files the init created are removed through saferm,
// so running it again starts where this run did.
func Init(e *strictcli.Effects, req InitRequest) error {
	if req.Say == nil {
		return errors.New("monorepo init needs somewhere to report to")
	}
	path := filepath.Join(req.Root, filepath.FromSlash(declarations.ReleasablesFile))
	if _, err := os.Lstat(path); err == nil {
		return fmt.Errorf("%s already exists: this repository already declares its members and releasables, and `rlsbl monorepo init` writes the declarations of a repository that declares none. Change the existing declarations with `rlsbl monorepo add` and `rlsbl monorepo remove`", declarations.ReleasablesFile)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("reading %s: %w", declarations.ReleasablesFile, err)
	}
	root := declarations.Member{Path: declarations.RootPath, Name: declarations.RootName}
	d := &declarations.Releasables{
		Layout:          declarations.LayoutWorkspace,
		ReleaseBranches: append([]string(nil), req.ReleaseBranches...),
		Members:         []declarations.Member{root},
	}
	if r := req.RootReleasable; r != nil {
		if r.PublishCICheckPattern != "" {
			if _, err := workflows.WaitForCIJob(r.PublishCICheckPattern); err != nil {
				return fmt.Errorf("--publish-ci-check-pattern: %w", err)
			}
		}
		d.Releasables = []declarations.Releasable{*r}
		d.Members[0].Releasable = r.Name
	} else {
		d.Members[0].DevOnly = true
	}
	data := declarations.Render(d)
	if _, err := declarations.Parse(data); err != nil {
		return err
	}

	manifest := declarations.ReleasablesDir + "/manifest.toml"
	created := []string{declarations.ReleasablesFile}
	if _, err := os.Lstat(filepath.Join(req.Root, filepath.FromSlash(manifest))); errors.Is(err, fs.ErrNotExist) {
		created = append(created, manifest)
	} else if err != nil {
		return fmt.Errorf("reading %s: %w", manifest, err)
	}
	if _, err := declarations.Write(e, req.Root, data); err != nil {
		return err
	}
	req.Say("Initialized the workspace: wrote " + declarations.ReleasablesFile + ".")
	if r := req.RootReleasable; r != nil {
		req.Say(fmt.Sprintf("The root member %q is versioned under the releasable %q (tag format %s, publish mode %s).", declarations.RootName, r.Name, r.TagFormat, r.PublishMode))
	} else {
		req.Say(fmt.Sprintf("The root member %q is a dev node: dev-only and versioned under no releasable.", declarations.RootName))
	}
	if !req.AutoCommit {
		req.Say("Not committed (--no-auto-commit): " + strings.Join(created, ", ") + ".")
		return nil
	}
	repo, err := git.Open(e, req.Root)
	if err != nil {
		return err
	}
	if _, err := repo.Commit(git.CommitRequest{Message: InitCommitMessage, Paths: created, RequireChange: true}); err != nil {
		// An uncommitted declarations file would make the next init refuse
		// as "already declared", so what this run created goes again.
		for _, p := range created {
			if rmErr := saferm.Delete(e, req.Root, saferm.Request{Path: p, Description: "rolling back a monorepo init whose commit failed", SkipMissing: true}); rmErr != nil {
				return fmt.Errorf("committing the declarations failed (%v), and removing %s again failed too (%v): remove it through saferm before running `rlsbl monorepo init` again", err, p, rmErr)
			}
		}
		return fmt.Errorf("committing the declarations failed (%v), so the workspace is not initialized: %s were removed through saferm. Fix what the commit reports, then run `rlsbl monorepo init` again", err, strings.Join(created, " and "))
	}
	req.Say("Committed: " + InitCommitMessage)
	return nil
}
