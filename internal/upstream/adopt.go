package upstream

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
)

// pushTimeout bounds each of the adoption's pushes.
const pushTimeout = 2 * time.Minute

// TagState is what the adoption does with one tag name.
type TagState string

// The tag states.
const (
	// Inherited is a tag that moves: the run writes something for it.
	Inherited TagState = "inherited"
	// Adopted is a tag already moved here and on origin: nothing to do.
	Adopted TagState = "adopted"
	// Conflict is a tag carrying an inherited tag's name at another
	// object: the whole run is refused.
	Conflict TagState = "conflict"
)

// TagPlan is one tag name's observed refs and what the adoption does with
// it. Inherited is the object the tag was inherited at: the kept ref's when
// an earlier adoption wrote one, else the upstream's own refs/tags/<tag>.
// The other objects are what each ref holds, empty when it is absent: Local
// and Origin are refs/tags/<tag> here and on origin, Kept and OriginKept the
// kept ref here and on origin.
type TagPlan struct {
	Tag        string
	State      TagState
	Inherited  string
	Local      string
	Origin     string
	Kept       string
	OriginKept string
	Problems   []string
}

// CreateKept reports whether the kept ref is written here.
func (t TagPlan) CreateKept() bool { return t.State == Inherited && t.Kept == "" }

// PushKept reports whether the kept ref is pushed to origin.
func (t TagPlan) PushKept() bool { return t.State == Inherited && t.OriginKept == "" }

// DeleteOrigin reports whether the tag is deleted from origin's refs/tags.
func (t TagPlan) DeleteOrigin() bool { return t.State == Inherited && t.Origin != "" }

// DeleteLocal reports whether the tag is deleted from refs/tags here.
func (t TagPlan) DeleteLocal() bool { return t.State == Inherited && t.Local != "" }

// MissingObject is an inherited tag whose object this repository does not
// hold, so its kept ref cannot be written here; OnlyKeptOnOrigin is set when
// only origin's kept ref names it.
type MissingObject struct {
	Tag              string
	OnlyKeptOnOrigin bool
}

// AdoptPlan is everything one adoption observed and would do.
type AdoptPlan struct {
	Upstream Upstream
	// Tags are the tag names the upstream has, in name order.
	Tags []TagPlan
	// Ours are the tag names the upstream does not have, which stay in
	// refs/tags.
	Ours           []string
	MissingObjects []MissingObject
}

// InState are the plan's tags in state s.
func (p AdoptPlan) InState(s TagState) []TagPlan {
	var out []TagPlan
	for _, t := range p.Tags {
		if t.State == s {
			out = append(out, t)
		}
	}
	return out
}

// NoUpstreamMessage is the refusal of adopt-tags in a repository declaring
// no upstream.
func NoUpstreamMessage() string {
	return fmt.Sprintf("this repository declares no upstream: %s does not exist, so it is not a fork and has no inherited tags.\n"+
		"  A fork declares its upstream there (strictspec's upstream schema, every field required), for example:\n%s\n"+
		"  then run this command again.", File, declarationExample)
}

// strip maps each ref under prefix to its object, keyed by the rest of its
// name.
func strip(refs map[string]string, prefix string) map[string]string {
	out := map[string]string{}
	for name, object := range refs {
		if rest, ok := strings.CutPrefix(name, prefix); ok {
			out[rest] = object
		}
	}
	return out
}

// short is an object id's first twelve characters, as the plan prints it.
func short(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}

// firstNonEmpty is the first of values that is not empty.
func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// Observe reads everything an adoption decides on and writes nothing:
// the declaration, this repository's refs/tags and kept refs, origin's
// (git ls-remote), and the upstream's tags (git ls-remote on the declared
// URL, never a package registry). repo is bound to an observer.
func Observe(repo git.Repo) (AdoptPlan, error) {
	u, found, err := Load(repo.Dir())
	if err != nil {
		return AdoptPlan{}, err
	}
	if !found {
		return AdoptPlan{}, errors.New(NoUpstreamMessage())
	}
	configured, err := repo.RemoteConfigured(Remote)
	if err != nil {
		return AdoptPlan{}, err
	}
	if !configured {
		return AdoptPlan{}, fmt.Errorf("this repository has no `%s` remote. The inherited tags are deleted from %s and their %s* refs pushed there so they are not lost, so the adoption needs it: add it (git remote add %s <url of this fork>), then run this command again", Remote, Remote, u.TagsOfPrefix(), Remote)
	}
	upstreamRefs, err := repo.RemoteRefs(u.URL(), tagsPrefix+"*")
	if err != nil {
		return AdoptPlan{}, err
	}
	originRefs, err := repo.RemoteRefs(Remote, tagsPrefix+"*", u.TagsOfPrefix()+"*")
	if err != nil {
		return AdoptPlan{}, err
	}
	localTagRefs, err := repo.LocalRefs(tagsPrefix)
	if err != nil {
		return AdoptPlan{}, err
	}
	localKeptRefs, err := repo.LocalRefs(u.TagsOfPrefix())
	if err != nil {
		return AdoptPlan{}, err
	}
	upstreamTags := strip(upstreamRefs, tagsPrefix)
	originTags, originKept := strip(originRefs, tagsPrefix), strip(originRefs, u.TagsOfPrefix())
	localTags, localKept := strip(localTagRefs, tagsPrefix), strip(localKeptRefs, u.TagsOfPrefix())

	names := map[string]bool{}
	for _, m := range []map[string]string{localTags, originTags, localKept, originKept} {
		for name := range m {
			names[name] = true
		}
	}
	sorted := make([]string, 0, len(names))
	for name := range names {
		sorted = append(sorted, name)
	}
	sort.Strings(sorted)

	plan := AdoptPlan{Upstream: u}
	for _, tag := range sorted {
		t := TagPlan{Tag: tag, Local: localTags[tag], Origin: originTags[tag], Kept: localKept[tag], OriginKept: originKept[tag]}
		// A kept ref records the object from an earlier adoption, which
		// checked it against the upstream then and is never rewritten if
		// the upstream later moves or deletes its tag.
		t.Inherited = firstNonEmpty(t.Kept, t.OriginKept, upstreamTags[tag])
		if t.Inherited == "" {
			plan.Ours = append(plan.Ours, tag)
			continue
		}
		if t.Kept != "" && t.OriginKept != "" && t.Kept != t.OriginKept {
			t.Problems = append(t.Problems, fmt.Sprintf("%s is at %s here and at %s on %s", u.KeptRef(tag), short(t.Kept), short(t.OriginKept), Remote))
		}
		source := "the upstream's"
		if t.Kept != "" || t.OriginKept != "" {
			source = fmt.Sprintf("the inherited one (%s)", u.KeptRef(tag))
		}
		if t.Local != "" && t.Local != t.Inherited {
			t.Problems = append(t.Problems, fmt.Sprintf("refs/tags/%s here is at %s, %s is at %s", tag, short(t.Local), source, short(t.Inherited)))
		}
		if t.Origin != "" && t.Origin != t.Inherited {
			t.Problems = append(t.Problems, fmt.Sprintf("refs/tags/%s on %s is at %s, %s is at %s", tag, Remote, short(t.Origin), source, short(t.Inherited)))
		}
		switch {
		case len(t.Problems) > 0:
			t.State = Conflict
		case t.Kept != "" && t.OriginKept != "" && t.Local == "" && t.Origin == "":
			t.State = Adopted
		default:
			t.State = Inherited
			if t.Kept == "" && t.Local == "" {
				held, err := repo.HasObject(t.Inherited)
				if err != nil {
					return AdoptPlan{}, err
				}
				if !held {
					plan.MissingObjects = append(plan.MissingObjects, MissingObject{Tag: tag, OnlyKeptOnOrigin: t.OriginKept != ""})
				}
			}
		}
		plan.Tags = append(plan.Tags, t)
	}
	return plan, nil
}

// Refusal says why the plan may not be applied, and is empty when it may.
// A conflict refuses the whole run before anything is written, and so does
// an inherited tag whose object is not here to write its kept ref at.
func Refusal(plan AdoptPlan) string {
	u := plan.Upstream
	var parts []string
	if conflicts := plan.InState(Conflict); len(conflicts) > 0 {
		var lines []string
		for _, t := range conflicts {
			for _, p := range t.Problems {
				lines = append(lines, fmt.Sprintf("    %s: %s", t.Tag, p))
			}
		}
		parts = append(parts, fmt.Sprintf("%d tag(s) carry an inherited tag's name at a different object. A tag is inherited only when the upstream (%s) has a tag of the same name at the same object, so these are not, and rlsbl moves no tag while one remains and will not guess which object the name should carry:\n%s",
			len(conflicts), u.URL(), strings.Join(lines, "\n")))
	}
	if len(plan.MissingObjects) > 0 {
		var fetches, names []string
		for _, m := range plan.MissingObjects {
			names = append(names, m.Tag)
			fetch := fmt.Sprintf("git fetch %s tag %s", Remote, m.Tag)
			if m.OnlyKeptOnOrigin {
				fetch = u.TagsOfFetchCommand()
			}
			if !contains(fetches, fetch) {
				fetches = append(fetches, fetch)
			}
		}
		parts = append(parts, fmt.Sprintf("%d inherited tag(s) exist only on %s (%s), and their objects are not in this repository, so their %s* refs cannot be written here. Fetch them, then run this command again:\n    %s",
			len(plan.MissingObjects), Remote, strings.Join(names, ", "), u.TagsOfPrefix(), strings.Join(fetches, "\n    ")))
	}
	return strings.Join(parts, "\n")
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// steps are what the adoption does for an inherited tag, in order.
func (t TagPlan) steps(u Upstream) []string {
	var steps []string
	if t.CreateKept() {
		steps = append(steps, "write "+u.KeptRef(t.Tag))
	}
	if t.PushKept() {
		steps = append(steps, "push it to "+Remote)
	}
	if t.DeleteOrigin() {
		steps = append(steps, fmt.Sprintf("delete refs/tags/%s on %s", t.Tag, Remote))
	}
	if t.DeleteLocal() {
		steps = append(steps, fmt.Sprintf("delete refs/tags/%s here", t.Tag))
	}
	return steps
}

// ownTagsKey is the preview key of the tags the upstream does not have; a
// tag name cannot hold a space, so it never collides with one.
const ownTagsKey = "(tags the upstream does not have)"

// Preview is the plan as previewapply items: one per tag the upstream has,
// then one for the tags that stay.
func (p AdoptPlan) Preview() (previewapply.Preview, error) {
	u := p.Upstream
	var items []previewapply.Item
	for _, t := range p.Tags {
		it := previewapply.Item{Key: t.Tag, State: string(t.State), Data: t}
		switch t.State {
		case Inherited:
			it.Summary = "at " + short(t.Inherited)
			for _, s := range t.steps(u) {
				it.Actions = append(it.Actions, "apply would "+s+".")
			}
		case Adopted:
			it.Summary = fmt.Sprintf("already in %s here and on %s", u.KeptRef(t.Tag), Remote)
		case Conflict:
			it.Summary = strings.Join(t.Problems, "; ")
		}
		items = append(items, it)
	}
	if len(p.Ours) > 0 {
		items = append(items, previewapply.Item{
			Key:     ownTagsKey,
			State:   "stay",
			Summary: fmt.Sprintf("%d tag(s) the upstream does not have stay in refs/tags: %s", len(p.Ours), strings.Join(p.Ours, ", ")),
		})
	}
	return previewapply.NewPreview(items...)
}

// moveTag performs one inherited tag's move: its kept ref is written here,
// pushed to origin, the tag deleted from origin, and then from here. Every
// push carries one ref, because GitHub fires no events for a push deleting
// four or more tags; each push is guarded by a lease on the object
// observed, and the local deletion by its object too. A tag is never
// deleted anywhere before its kept ref is on origin, so a run that stops
// part-way leaves every earlier tag finished, which the next run's
// observation sees.
func moveTag(repo git.Repo, u Upstream, t TagPlan) error {
	fail := func(what string, err error) error {
		return fmt.Errorf("could not %s: %w\n  Nothing already done is undone, and every step is guarded by the object it expects: run `rlsbl upstream adopt-tags` again to finish", what, err)
	}
	kept := u.KeptRef(t.Tag)
	if t.CreateKept() {
		if err := repo.CreateRef(kept, t.Inherited); err != nil {
			return fail("write "+kept, err)
		}
	}
	if t.PushKept() {
		if err := repo.Push(Remote, git.RefUpdate{Ref: kept, New: t.Inherited}, pushTimeout); err != nil {
			return fail(fmt.Sprintf("push %s to %s", kept, Remote), err)
		}
	}
	if t.DeleteOrigin() {
		if err := repo.Push(Remote, git.RefUpdate{Ref: tagsPrefix + t.Tag, Expected: t.Origin}, pushTimeout); err != nil {
			return fail(fmt.Sprintf("delete %s%s on %s", tagsPrefix, t.Tag, Remote), err)
		}
	}
	if t.DeleteLocal() {
		if err := repo.DeleteRef(tagsPrefix+t.Tag, t.Local); err != nil {
			return fail(fmt.Sprintf("delete %s%s", tagsPrefix, t.Tag), err)
		}
	}
	return nil
}

// RunAdoptTags is `rlsbl upstream adopt-tags` in the repository rooted at
// root: observe, refuse a conflict or a missing object before anything is
// written, print the plan under --dry-run, and otherwise move each
// inherited tag in turn.
func RunAdoptTags(ctx *strictcli.Context, root string) error {
	var plan AdoptPlan
	var moved []TagPlan
	_, err := previewapply.Reconcile(ctx, previewapply.Reconciler{
		Observe: func(o previewapply.Observer) (previewapply.Preview, error) {
			repo, err := git.Open(o, root)
			if err != nil {
				return previewapply.Preview{}, err
			}
			plan, err = Observe(repo)
			if err != nil {
				return previewapply.Preview{}, err
			}
			if refusal := Refusal(plan); refusal != "" {
				return previewapply.Preview{}, errors.New(refusal)
			}
			return plan.Preview()
		},
		Apply: func(e *strictcli.Effects, it previewapply.Item) error {
			t, ok := it.Data.(TagPlan)
			if !ok || t.State != Inherited {
				return nil
			}
			repo, err := git.Open(e, root)
			if err != nil {
				return err
			}
			if err := moveTag(repo, plan.Upstream, t); err != nil {
				return err
			}
			moved = append(moved, t)
			ctx.Info(fmt.Sprintf("  %s: moved to %s", t.Tag, plan.Upstream.KeptRef(t.Tag)))
			return nil
		},
		ShowKeys: true,
	})
	if err != nil {
		return err
	}
	u := plan.Upstream
	work := plan.InState(Inherited)
	switch {
	case len(work) == 0:
		ctx.Info(fmt.Sprintf("Nothing to do: no tag in refs/tags here or on %s is inherited from %s.", Remote, u.URL()))
	case ctx.DryRun():
		ctx.Info(fmt.Sprintf("Dry run: %d inherited tag(s) would move to %s. Nothing was written.", len(work), u.TagsOfPrefix()))
	default:
		counts := []struct {
			n    int
			what string
		}{
			{countOf(moved, TagPlan.CreateKept), "kept ref(s) written here"},
			{countOf(moved, TagPlan.PushKept), "kept ref(s) pushed to " + Remote},
			{countOf(moved, TagPlan.DeleteOrigin), "tag(s) deleted from " + Remote + "'s refs/tags"},
			{countOf(moved, TagPlan.DeleteLocal), "tag(s) deleted from refs/tags here"},
		}
		var done []string
		for _, c := range counts {
			if c.n > 0 {
				done = append(done, fmt.Sprintf("%d %s", c.n, c.what))
			}
		}
		ctx.Info(fmt.Sprintf("Adopted %d inherited tag(s) from %s under %s: %s.", len(moved), u.URL(), u.TagsOfPrefix(), strings.Join(done, ", ")))
	}
	return nil
}

// countOf counts the tags for which step is true.
func countOf(tags []TagPlan, step func(TagPlan) bool) int {
	n := 0
	for _, t := range tags {
		if step(t) {
			n++
		}
	}
	return n
}
