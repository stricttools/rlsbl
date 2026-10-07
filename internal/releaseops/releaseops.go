// Package releaseops is rlsbl's commands on past releases: `release edit`
// rewrites one version's GitHub Release from the record, `release retry`
// dispatches a release's workflows again at its tag, `release undo` reverts
// a release once the evidence shows nothing published it, `release abandon`
// records an abandoned attempt's version as never released, and `release
// deprecate` and `release yank` put a notice on a past version, yank also
// performing each registry's own removal (npm deprecate, a Go retract
// directive, PyPI's manual yank), and never unpublishing anything.
//
// Every command selects the releasable of the member whose directory holds
// the working directory, reads everything it decides from before it writes
// anything, and records a notice in the version's archive and commits it
// before it touches the GitHub Release, so every later rewrite of the
// Release keeps it.
package releaseops

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/upstream"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// origin is the remote every release pushes to and every command here
// reads.
const origin = "origin"

// Selection is the releasable the working directory selects, in its
// repository.
type Selection struct {
	Repo       git.Repo
	Workspace  *workspace.Workspace
	Member     declarations.Member
	Releasable declarations.Releasable
	Scheme     workspace.TagScheme
	Record     *releaserecord.Record
	e          *strictcli.Effects
}

// Select reads the repository holding the absolute directory dir and the
// releasable of the member whose territory holds dir. A member versioned
// under no releasable is refused: it has no releases to act on.
func Select(e *strictcli.Effects, dir string) (Selection, error) {
	ws, err := workspace.Discover(dir)
	if err != nil {
		return Selection{}, err
	}
	repo, err := git.Open(e, ws.Root)
	if err != nil {
		return Selection{}, err
	}
	member, err := ws.MemberAtDirectory(dir)
	if err != nil {
		return Selection{}, err
	}
	r, ok := ws.ReleasableOf(member)
	if !ok {
		return Selection{}, fmt.Errorf("the member %q (path %q) is versioned under no releasable, so it has no releases to act on: run this from the directory of a member that is versioned under one", member.Name, member.Path)
	}
	scheme, err := workspace.SchemeOf(r)
	if err != nil {
		return Selection{}, err
	}
	upstreamURL, err := upstream.URLOf(ws.Root)
	if err != nil {
		return Selection{}, err
	}
	return Selection{
		Repo:       repo,
		Workspace:  ws,
		Member:     member,
		Releasable: r,
		Scheme:     scheme,
		Record:     releaserecord.New(repo, r.Name, scheme, upstreamURL),
		e:          e,
	}, nil
}

// effects is the effects handle the selection was read through.
func (s Selection) effects() *strictcli.Effects { return s.e }

// Root is the repository root.
func (s Selection) Root() string { return s.Workspace.Root }

// abs is the absolute path of the repository-relative rel.
func (s Selection) abs(rel string) string { return filepath.Join(s.Root(), filepath.FromSlash(rel)) }

// pushTimeout bounds one push, as the release bounds its own: the
// declarations' push_seconds, else the shipped push timeout.
func (s Selection) pushTimeout() time.Duration {
	timeout, _ := release.PushTimeout(s.Workspace.Declarations, 0, false)
	return timeout
}

// ParseVersion reads a version argument. A version is MAJOR.MINOR.PATCH,
// and a spelling with a leading "v" is refused naming the bare one, since
// the caller's spelling is never tidied into another.
func ParseVersion(text, what string) (semver.Version, error) {
	if rest, ok := strings.CutPrefix(text, "v"); ok && semver.Valid(rest) {
		return semver.Version{}, fmt.Errorf("%s %q carries a leading \"v\"; name the version bare: %s", what, text, rest)
	}
	v, err := semver.Parse(text)
	if err != nil {
		return semver.Version{}, fmt.Errorf("%s: %w", what, err)
	}
	return v, nil
}

// archive reads the archive of v, and found false when there is none.
func (s Selection) archive(v semver.Version) (releaserecord.Archive, bool, error) {
	rel := releaserecord.ArchivePath(s.Record.Dir(), v)
	if _, err := os.Lstat(s.abs(rel)); errors.Is(err, os.ErrNotExist) {
		return releaserecord.Archive{}, false, nil
	} else if err != nil {
		return releaserecord.Archive{}, false, fmt.Errorf("reading %s: %w", rel, err)
	}
	a, err := releaserecord.ReadArchive(s.Root(), s.Record.Dir(), v)
	if err != nil {
		return releaserecord.Archive{}, false, err
	}
	return a, true, nil
}

// releasedArchive reads the archive of a version a command acts on as a
// release: one that exists and records a release, recorded or
// unrecoverable. Every other state is refused, naming what the record says.
func (s Selection) releasedArchive(v semver.Version) (releaserecord.Archive, error) {
	a, found, err := s.archive(v)
	if err != nil {
		return releaserecord.Archive{}, err
	}
	rel := releaserecord.ArchivePath(s.Record.Dir(), v)
	if !found {
		return releaserecord.Archive{}, fmt.Errorf("%s of %s has no release archive at %s, so the record holds no release under it. A notice or an undo is recorded against the version's archive; if %s was released, backfill its archive first, previewing it: `rlsbl release backfill --dry-run`, then `rlsbl release backfill --approve-consequential`", v, s.Releasable.Name, rel, v)
	}
	switch a.Fate {
	case releaserecord.FateNeverReleased:
		return releaserecord.Archive{}, fmt.Errorf("%s records that no release was ever published under %s (never_released = true), so there is no release of it to act on", rel, v)
	case releaserecord.FateUnstated:
		return releaserecord.Archive{}, fmt.Errorf("%s states no fate (recorded, unrecoverable, or never released), so whether %s shipped cannot be told. Backfill it, previewing first: `rlsbl release backfill --dry-run`, then `rlsbl release backfill --approve-consequential`", rel, v)
	}
	return a, nil
}

// tagOf is the tag a released version shipped under: its archive's
// shipped_as, or the releasable's tag format rendered at it.
func (s Selection) tagOf(a releaserecord.Archive) string {
	if a.ShippedAs != "" {
		return a.ShippedAs
	}
	return s.Scheme.Render(a.Version)
}

// latestReleased is the highest version the archives record as released,
// and false when none is.
func (s Selection) latestReleased() (semver.Version, bool, error) {
	return releaserecord.LatestReleasedVersion(s.Root(), s.Record.Dir())
}

// openGitHub is the gh client and the GitHub repository the releasable's
// Releases live in: the declarations' github_repository, or the one origin
// names. gh must be installed and authenticated.
func openGitHub(e *strictcli.Effects, s Selection) (github.Client, github.Repository, error) {
	originURL := ""
	configured, err := s.Repo.RemoteConfigured(origin)
	if err != nil {
		return github.Client{}, github.Repository{}, err
	}
	if configured {
		if originURL, err = s.Repo.RemoteURL(origin); err != nil {
			return github.Client{}, github.Repository{}, err
		}
	}
	slug, err := github.ResolveRepository(s.Workspace.Declarations.GitHubRepository, originURL)
	if err != nil {
		return github.Client{}, github.Repository{}, err
	}
	gh, err := github.New(e)
	if err != nil {
		return github.Client{}, github.Repository{}, err
	}
	if err := gh.CheckAuth(); err != nil {
		return github.Client{}, github.Repository{}, err
	}
	return gh, slug, nil
}

// requireRelease refuses a version whose GitHub Release does not exist.
func requireRelease(gh github.Client, slug github.Repository, tag string) error {
	exists, err := gh.ReleaseExists(slug, tag)
	if err != nil {
		return err
	}
	if !exists {
		return fmt.Errorf("%s has no GitHub Release for %s. Nothing was changed. `rlsbl release reconcile --mode plan`, then `--mode apply`, creates the Releases the record holds and GitHub lacks", slug, tag)
	}
	return nil
}

// lock takes the repository's advisory lock, refusing when another rlsbl
// process holds it: every command here writes release state a release in
// progress also writes.
func lock(ctx *strictcli.Context, root string) (*runstate.Lock, error) {
	return runstate.Acquire(ctx.Effects(), root, runstate.AcquireOptions{DryRun: ctx.DryRun(), Wait: runstate.RefuseWhenHeld})
}

// unlock gives the lock back, joining its error to err.
func unlock(l *runstate.Lock, err *error) {
	if rerr := l.Release(); rerr != nil {
		*err = errors.Join(*err, rerr)
	}
}

// commit commits paths with the Autogenerated trailer when the command is
// not a preview. Under --dry-run the commit is not attempted: the writes
// before it were recorded rather than performed, so there is nothing on disk
// to commit, and the preview says it would.
func commit(ctx *strictcli.Context, s Selection, message string, paths []string, autogenerated bool) error {
	if ctx.DryRun() {
		ctx.Info(fmt.Sprintf("would commit %s: %q", strings.Join(paths, ", "), message))
		return nil
	}
	_, err := s.Repo.Commit(git.CommitRequest{Message: message, Paths: paths, Autogenerated: autogenerated, RequireChange: true})
	return err
}
