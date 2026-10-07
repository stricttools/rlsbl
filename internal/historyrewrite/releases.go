package historyrewrite

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releasenotes"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// ReleaseRepair is what rewriting the GitHub Releases of the tags a rewrite
// moved reads and writes through.
type ReleaseRepair struct {
	Repo       git.Repo
	Workspace  *workspace.Workspace
	Record     *lifecycle.Record
	GitHub     github.Client
	Repository github.Repository
	// Rewrites is the rewrite's commit map, through which the marker of a
	// version whose record names no release commit is moved.
	Rewrites map[string]string
	// Say prints one line of the operator's report.
	Say func(string)
}

// tagRelease is the version a tag's GitHub Release documents, or why the tag
// carries none.
type tagRelease struct {
	refs    *releasableRefs
	version semver.Version
	// skip, when set, says why no Release document is written for the tag.
	skip string
}

// releaseResolver decides, per tag, whose version's Release the tag carries.
type releaseResolver struct {
	ws    *workspace.Workspace
	refs  map[string]*releasableRefs
	order []string
	// shippedAs maps a tag an archive records in shipped_as to its releasable
	// and version.
	shippedAs    map[string]tagRelease
	explanations *releaserecord.Explanations
	created      map[string]time.Time
}

func newReleaseResolver(repo git.Repo, ws *workspace.Workspace, record *lifecycle.Record) (*releaseResolver, error) {
	events, err := releaserecord.ReadEvents(ws.Root)
	if err != nil {
		return nil, err
	}
	res := &releaseResolver{ws: ws, refs: map[string]*releasableRefs{}, shippedAs: map[string]tagRelease{}}
	for _, r := range ws.Releasables() {
		x, err := newReleasableRefs(ws, events, r)
		if err != nil {
			return nil, err
		}
		res.refs[r.Name] = x
		res.order = append(res.order, r.Name)
		versions, err := releaserecord.ArchivedVersions(ws.Root, x.archiveDir())
		if err != nil {
			return nil, err
		}
		for _, v := range versions {
			a, err := releaserecord.ReadArchive(ws.Root, x.archiveDir(), v)
			if err != nil {
				return nil, err
			}
			if a.ShippedAs != "" {
				res.shippedAs[a.ShippedAs] = tagRelease{refs: x, version: v}
			}
		}
	}
	dirs, err := releaserecord.ArchiveDirs(ws.Root)
	if err != nil {
		return nil, err
	}
	if res.explanations, err = releaserecord.BuildExplanations(ws.Root, nil, dirs, record); err != nil {
		return nil, err
	}
	if res.created, err = repo.TagCreationTimes(); err != nil {
		return nil, err
	}
	return res, nil
}

// resolve decides whose Release tag carries: the archive recording it in
// shipped_as, else the releasable whose tag format renders it. A companion
// tag, an unversioned tag, and a closed identity's tag carry none. Any other
// tag is refused: whose notes and release commit it carries cannot be
// guessed.
func (res *releaseResolver) resolve(tag string) (tagRelease, error) {
	if found, ok := res.shippedAs[tag]; ok {
		return found, nil
	}
	owner, owned, err := res.ws.TagOwner(tag)
	if err != nil {
		return tagRelease{}, err
	}
	if owned {
		x := res.refs[owner.Name]
		v, _ := x.scheme.VersionOf(tag)
		return tagRelease{refs: x, version: v}, nil
	}
	if v, _, isVersion := workspace.ParseVersionTag(tag); isVersion {
		for _, name := range res.order {
			companion, err := res.refs[name].isCompanion(tag, v)
			if err != nil {
				return tagRelease{}, err
			}
			if companion {
				return tagRelease{skip: fmt.Sprintf("a companion tag of %s %s, which carries no GitHub Release", name, v)}, nil
			}
		}
	}
	created, known := res.created[tag]
	if known {
		x, explained, err := res.explanations.Explain(tag, created)
		if err != nil {
			return tagRelease{}, err
		}
		if explained {
			return tagRelease{skip: x.Describe() + "; no Release document of a current releasable is written for it"}, nil
		}
	}
	return tagRelease{}, unownedTagError(tag)
}

func unownedTagError(tag string) error {
	return fmt.Errorf("the tag %s belongs to no releasable here: no tag format renders it, no archive records it in shipped_as, and the lifecycle-and-license record does not account for it, so whose notes and release commit its Release carries cannot be told. If it is no release tag, record it with `rlsbl transition unversioned-tag --tag %s --reason \"<why>\"`; if it is one, correct the tag_format of the releasable it belongs to in %s", tag, tag, declarations.ReleasablesFile)
}

// RewriteReleases writes the GitHub Release document of every tag in tags
// from the record, after a rewrite moved the tags: a Release follows its
// tag's name, so it already sits on the rewritten commit, but its body (the
// notes and the rlsbl-ci-sha marker the publish workflow reads) and its
// pre-release flag do not follow. An existing Release is rewritten in place,
// and a tag carrying none gets one; nothing is ever deleted, so a failure
// leaves the previous Release standing. Every tag is attempted, and the
// failures are returned together: running the rewrite's command again
// writes the documents again. It returns how many Releases it wrote.
func RewriteReleases(r ReleaseRepair, tags []TagRewrite) (int, error) {
	if len(tags) == 0 {
		return 0, nil
	}
	if err := r.GitHub.CheckInstalled(); err != nil {
		return 0, fmt.Errorf("the GitHub Releases of the moved tags cannot be rewritten: %w", err)
	}
	if err := r.GitHub.CheckAuth(); err != nil {
		return 0, fmt.Errorf("the GitHub Releases of the moved tags cannot be rewritten: %w", err)
	}
	res, err := newReleaseResolver(r.Repo, r.Workspace, r.Record)
	if err != nil {
		return 0, err
	}
	written := 0
	var failures []string
	for _, t := range tags {
		tag, ok := tagName(t.Refname)
		if !ok {
			failures = append(failures, fmt.Sprintf("%s: not a tag ref (refs/tags/<name>), so it carries no Release", t.Refname))
			continue
		}
		wrote, err := r.rewriteOne(res, tag)
		if err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", tag, err))
			continue
		}
		if wrote {
			written++
		}
	}
	if len(failures) > 0 {
		return written, fmt.Errorf("%d GitHub Release(s) could not be written; nothing was deleted, so each still carries its previous document. Fix the causes and run the same command again, which writes them again:\n  %s", len(failures), strings.Join(failures, "\n  "))
	}
	return written, nil
}

// rewriteOne writes the Release document of one moved tag, reporting
// whether it wrote one.
func (r ReleaseRepair) rewriteOne(res *releaseResolver, tag string) (bool, error) {
	found, err := res.resolve(tag)
	if err != nil {
		return false, err
	}
	if found.skip != "" {
		r.Say(fmt.Sprintf("%s: %s", tag, found.skip))
		return false, nil
	}
	x := found.refs
	fate, err := archiveFate(r.Workspace.Root, x.archiveDir(), found.version)
	if err != nil {
		return false, err
	}
	if fate == releaserecord.FateNeverReleased {
		r.Say(fmt.Sprintf("%s: %s %s is recorded never released, so no GitHub Release is owed under it", tag, x.releasable.Name, found.version))
		return false, nil
	}
	doc, err := releasenotes.Read(r.Workspace.Root, x.releasable.Name, x.scheme, found.version)
	if err != nil {
		return false, err
	}
	if doc.Tag != tag {
		r.Say(fmt.Sprintf("%s: %s %s shipped under %s, whose Release carries its document", tag, x.releasable.Name, found.version, doc.Tag))
		return false, nil
	}
	exists, err := r.GitHub.ReleaseExists(r.Repository, tag)
	if err != nil {
		return false, err
	}
	if doc.ReleaseCommit == "" && exists {
		// The record names no release commit (unrecoverable), so the body's
		// own marker is kept, moved through the rewrite like every commit.
		body, err := r.GitHub.ReleaseBody(r.Repository, tag)
		if err != nil {
			return false, err
		}
		if sha, ok := github.CISHAFromBody(body); ok {
			doc.ReleaseCommit = sha
			if moved, ok := r.Rewrites[sha]; ok {
				doc.ReleaseCommit = moved
			}
		}
	}
	if exists {
		if err := releasenotes.Rewrite(r.GitHub, r.Repository, doc); err != nil {
			return false, err
		}
		r.Say(fmt.Sprintf("%s: rewrote its GitHub Release in place", tag))
		return true, nil
	}
	moves, err := releasenotes.RepairTakesLatest(r.GitHub, r.Repository, doc, func(latest string) (bool, error) {
		return releasenotes.TagNewerInHistory(r.Repo, tag, latest)
	})
	if err != nil {
		return false, err
	}
	if err := releasenotes.Create(r.GitHub, r.Repository, doc, moves); err != nil {
		return false, err
	}
	r.Say(fmt.Sprintf("%s: created its missing GitHub Release", tag))
	return true, nil
}

// archiveFate is the fate the archive of v in dir states, FateAbsent when
// there is none.
func archiveFate(root, dir string, v semver.Version) (releaserecord.Fate, error) {
	versions, err := releaserecord.ArchivedVersions(root, dir)
	if err != nil {
		return "", err
	}
	for _, a := range versions {
		if semver.Compare(a, v) == 0 {
			archive, err := releaserecord.ReadArchive(root, dir, v)
			if err != nil {
				return "", err
			}
			return archive.Fate, nil
		}
	}
	return releaserecord.FateAbsent, nil
}

// sortedKeys are m's keys in order.
func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
