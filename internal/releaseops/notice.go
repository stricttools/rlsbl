package releaseops

import (
	"fmt"
	"slices"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releasenotes"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
)

// The notices' labels.
const (
	deprecatedLabel = "Deprecated"
	yankedLabel     = "Yanked"
)

// NoticeRequest is a deprecate or yank of one past version.
type NoticeRequest struct {
	// Dir is the working directory, absolute.
	Dir     string
	Version string
	// Reason is why, and empty when not given.
	Reason string
	// Use is the version to use instead, and empty when not given.
	Use string
}

// Notice is the line a deprecate or yank puts on top of a Release body:
// "> **<label>:** <reason>. Use v<use> instead." with either part left out
// when not given, and "> **<label>.**" with neither. A reason's own final
// periods are dropped, so the sentence ends with one.
func Notice(label, reason string, use *semver.Version) string {
	var parts []string
	if r := strings.TrimRight(strings.TrimSpace(reason), ". "); r != "" {
		parts = append(parts, r)
	}
	if use != nil {
		parts = append(parts, fmt.Sprintf("Use v%s instead", use))
	}
	if len(parts) == 0 {
		return fmt.Sprintf("> **%s.**", label)
	}
	return fmt.Sprintf("> **%s:** %s.", label, strings.Join(parts, ". "))
}

// noticeTarget is a past version a notice goes on, read and checked before
// anything is written.
type noticeTarget struct {
	s       Selection
	gh      github.Client
	slug    github.Repository
	version semver.Version
	archive releaserecord.Archive
	tag     string
	notice  string
}

// prepareNotice reads and checks everything a notice on req's version
// needs: the version and the replacement it names, the version's archive
// recording a release, a GitHub Release under its tag, and that it is not
// the releasable's latest release. Nothing is written.
func prepareNotice(ctx *strictcli.Context, s Selection, req NoticeRequest, label, operation string) (noticeTarget, error) {
	v, err := ParseVersion(req.Version, "the version")
	if err != nil {
		return noticeTarget{}, err
	}
	var use *semver.Version
	if req.Use != "" {
		u, err := ParseVersion(req.Use, "--use")
		if err != nil {
			return noticeTarget{}, err
		}
		fate, err := s.Record.Fate(u)
		if err != nil {
			return noticeTarget{}, err
		}
		if !fate.Released() {
			return noticeTarget{}, fmt.Errorf("--use names %s, which the release record of %s does not hold as released (%s), so the notice would send readers to a version nobody can install; name a released version", u, s.Releasable.Name, fate)
		}
		use = &u
	}
	a, err := s.releasedArchive(v)
	if err != nil {
		return noticeTarget{}, err
	}
	gh, slug, err := openGitHub(ctx.Effects(), s)
	if err != nil {
		return noticeTarget{}, err
	}
	tag := s.tagOf(a)
	if err := requireRelease(gh, slug, tag); err != nil {
		return noticeTarget{}, err
	}
	if err := s.refuseLatest(v, tag, operation); err != nil {
		return noticeTarget{}, err
	}
	return noticeTarget{s: s, gh: gh, slug: slug, version: v, archive: a, tag: tag, notice: Notice(label, req.Reason, use)}, nil
}

// publish records the notice in the version's archive and commits it, then
// rewrites the GitHub Release from the record, marking it pre-release. The
// archive is committed before GitHub is touched: a failed rewrite leaves the
// notice recorded, and `rlsbl release edit <version>` puts it on the
// Release. A notice the archive already holds is not recorded twice, so a
// run after an interrupted one finishes it.
func (n noticeTarget) publish(ctx *strictcli.Context, verb string) error {
	if !slices.Contains(n.archive.ReleaseNotices, n.notice) {
		if err := releaserecord.RecordReleaseNotice(ctx.Effects(), n.s.Root(), n.s.Record.Dir(), n.version, n.notice); err != nil {
			return err
		}
		if err := commit(ctx, n.s, fmt.Sprintf("release: %s %s", verb, n.tag), []string{n.archive.Path}, true); err != nil {
			return fmt.Errorf("the %s notice of %s was written into %s, and committing it failed: %w. Commit that file, then run `rlsbl release edit %s` to put the notice on the Release", verb, n.tag, n.archive.Path, err, n.version)
		}
	}
	doc, err := releasenotes.Read(n.s.Root(), n.s.Releasable.Name, n.s.Scheme, n.version)
	if err != nil {
		return err
	}
	// Under --dry-run the archive write was recorded, not made, so the
	// document read back lacks the notice; it is put where the record will
	// hold it.
	if !slices.Contains(doc.Notices, n.notice) {
		doc.Notices = append([]string{n.notice}, doc.Notices...)
	}
	return releasenotes.Rewrite(n.gh, n.slug, doc)
}

// Deprecate marks a past release deprecated: its notice is recorded in the
// version's archive and committed, and the GitHub Release is rewritten from
// the record with the notice on top and the pre-release flag set. The
// latest release, a version the record holds no release of, and a version
// without a GitHub Release are refused before anything is written.
func Deprecate(ctx *strictcli.Context, req NoticeRequest) (err error) {
	s, err := Select(ctx.Effects(), req.Dir)
	if err != nil {
		return err
	}
	l, err := lock(ctx, s.Root())
	if err != nil {
		return err
	}
	defer unlock(l, &err)
	n, err := prepareNotice(ctx, s, req, deprecatedLabel, "deprecate")
	if err != nil {
		return err
	}
	if err := n.publish(ctx, "deprecate"); err != nil {
		return err
	}
	if ctx.DryRun() {
		ctx.Out(fmt.Sprintf("Would mark %s pre-release with the notice:\n%s", n.tag, n.notice))
		return nil
	}
	ctx.Out(fmt.Sprintf("Deprecated %s (marked pre-release):\n%s", n.tag, n.notice))
	return nil
}
