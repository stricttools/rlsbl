package releaserecord_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The releasable every fixture releases, and its tag scheme.
const releasable = "gadget"

func scheme(t *testing.T, pattern string) workspace.TagScheme {
	t.Helper()
	s, err := workspace.NewTagScheme(pattern)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func version(t *testing.T, s string) semver.Version {
	t.Helper()
	v, err := semver.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

// releaseFile is a valid release file's text.
const releaseFile = `format_version = 2
bump = "minor"
include = ["npm"]
exclude = []
description = "the next release"
`

// writeArchive writes an archive of the fixture releasable directly, the way
// a repository's history would hold one. fate is "recorded" (with commit),
// "unrecoverable", "never-released", or "unstated".
func writeArchive(t *testing.T, root, v, fate, commit string) string {
	t.Helper()
	return writeArchiveWith(t, root, v, fate, commit, "")
}

// writeArchiveWith writes an archive whose top-level fields add extra.
func writeArchiveWith(t *testing.T, root, v, fate, commit, extra string) string {
	t.Helper()
	body := releaseFile + extra
	switch fate {
	case "recorded":
		body += "release_commit = \"" + commit + "\"\n\n[released_trees]\n\".\" = \"" + strings.Repeat("e", 40) + "\"\n"
	case "unrecoverable":
		body += "unrecoverable = true\n"
	case "never-released":
		body += "never_released = true\n"
	case "unstated":
	default:
		t.Fatalf("unknown fate %q", fate)
	}
	rel := releaserecord.ArchivePath(releaserecord.ArchiveDir(releasable), version(t, v))
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), body)
	return rel
}

// reading runs fn in a read_only command with rlsbl's observe allowlist, so
// every git read is an allowlisted observe, and returns fn's error.
func reading(t *testing.T, dir string, fn func(r *releaserecord.Record) error) error {
	t.Helper()
	return readingScheme(t, dir, "v{version}", fn)
}

func readingScheme(t *testing.T, dir, pattern string, fn func(r *releaserecord.Record) error) error {
	t.Helper()
	return readingFork(t, dir, pattern, "", fn)
}

// readingFork reads the record of a repository that is a fork of upstream.
func readingFork(t *testing.T, dir, pattern, upstream string, fn func(r *releaserecord.Record) error) error {
	t.Helper()
	s := scheme(t, pattern)
	var ferr error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		repo, err := git.Open(ctx.Effects(), dir)
		if err != nil {
			return err
		}
		ferr = fn(releaserecord.New(repo, releasable, s, upstream))
		return nil
	})
	return ferr
}

// writing runs fn in a mutating command and returns the dispatch's result
// and fn's error.
func writing(t *testing.T, dir string, dryRun bool, fn func(e *strictcli.Effects, repo git.Repo) error) (strictcli.Result, error) {
	t.Helper()
	var ferr error
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		repo, err := git.Open(ctx.Effects(), dir)
		if err != nil {
			return err
		}
		ferr = fn(ctx.Effects(), repo)
		return nil
	})
	return res, ferr
}

func mustNotFail(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func readText(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func requireContains(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
}

// readError asserts err is a record read error of reason and returns its
// message.
func readError(t *testing.T, err error, reason releaserecord.ReadErrorReason) string {
	t.Helper()
	re, ok := err.(*releaserecord.ReadError)
	if !ok {
		t.Fatalf("want a %s read error, got %T: %v", reason, err, err)
	}
	if re.Reason != reason {
		t.Fatalf("want a %s read error, got %s: %v", reason, re.Reason, err)
	}
	return re.Message
}

// errorOf is the error of a two-result call, for one-line readers.
func errorOf[T any](_ T, err error) error { return err }
