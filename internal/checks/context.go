package checks

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/upstream"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// ShippedCheckTimeout bounds each program a check starts when the
// declarations state no check_seconds and the caller states no budget.
const ShippedCheckTimeout = 900 * time.Second

// Inputs are what one check run is built from. Every field is the caller's
// statement; NewContext refuses the ones it cannot do without.
type Inputs struct {
	// Dir is the absolute directory the run stands in.
	Dir string
	// Releasable names the releasable the run answers for. Empty means the
	// one Dir selects: the releasable of the member whose territory Dir
	// lies in, or none at a workspace root.
	Releasable string
	// Now is the date the lifecycle-and-license record is judged on.
	Now time.Time
	// CheckTimeout bounds each program a check starts; zero takes the
	// declarations' check_seconds, or ShippedCheckTimeout when they state
	// none.
	CheckTimeout time.Duration
	// PushLines are the pre-push hook's ref lines; nil outside a push.
	PushLines []string
	// Home is the user's home directory, where ~/.npmrc is read.
	Home string
	// IndexPath is the machine-local confidential-name index.
	IndexPath string
	// UVProjectEnvironment is UV_PROJECT_ENVIRONMENT as the caller read it
	// (empty when unset): uv's own relocation of a project's environment,
	// where the dev overlays are installed.
	UVProjectEnvironment string
	// Scratch is the command's scratch directory, where the programs a check
	// runs write for themselves (npm's cache when an npm upload is listed);
	// nil when the command declares none, which refuses those checks.
	Scratch targets.Scratch
}

// Context is what every check of one run sees: the repository, its
// declarations, options, and lifecycle-and-license record, and the member
// and releasable the run answers for. It is strictcli's CheckContext.
type Context struct {
	e    *strictcli.Effects
	in   Inputs
	root string
	repo git.Repo

	ws      *workspace.Workspace
	declErr error
	opts    *options.Options
	optsErr error
	record  *lifecycle.Record
	recErr  error
	// upstreamURL and upstreamExclude are the fork's upstream and the
	// revisions of the history it inherited, empty for a repository that is
	// no fork.
	upstreamURL     string
	upstreamExclude []string
	upstreamErr     error

	// member is the member the run answers for: the one Dir lies in, or the
	// first member of the releasable Inputs names.
	member declarations.Member
	// releasable is the releasable the run answers for; nil for a member
	// versioned under none and at a workspace root.
	releasable *declarations.Releasable
	// unselected names every releasable of a workspace whose root the run
	// stands at with no releasable named; nil anywhere else. A check that
	// answers for one releasable refuses under it.
	unselected []string
	// members are the members a check sees, narrowed by its scope.
	members []declarations.Member
}

// NewContext builds the context of one check run through the effects
// handle e. A directory outside every git repository, and a releasable the
// declarations do not declare, are refused. Declarations, options, or a
// lifecycle-and-license record that cannot be read are kept as the errors
// the checks report: declarations-valid and lifecycle-record-valid name
// them, and every other check that needs them refuses to answer.
func NewContext(e *strictcli.Effects, in Inputs) (*Context, error) {
	switch {
	case e == nil:
		return nil, errors.New("checks: no effects handle to run checks through")
	case !filepath.IsAbs(in.Dir):
		return nil, fmt.Errorf("checks: the directory the checks run in, %q, is not absolute", in.Dir)
	case in.Now.IsZero():
		return nil, errors.New("checks: no date was given to judge the lifecycle-and-license record on")
	case in.CheckTimeout < 0:
		return nil, fmt.Errorf("checks: the check timeout %s is negative", in.CheckTimeout)
	}
	root, err := declarations.FindRepositoryRoot(in.Dir)
	if err != nil {
		return nil, err
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return nil, err
	}
	c := &Context{e: e, in: in, root: root, repo: repo}
	c.record, c.recErr = lifecycle.Load(root)
	if c.upstreamURL, c.upstreamErr = upstream.URLOf(root); c.upstreamErr == nil {
		c.upstreamExclude, c.upstreamErr = upstream.HistoryExclusions(repo)
	}
	c.ws, c.declErr = workspace.Load(root)
	if c.declErr != nil {
		return c, nil
	}
	reg, err := options.Shipped()
	if err != nil {
		return nil, err
	}
	c.opts, c.optsErr = options.Load(reg, root, c.ws.Declarations)
	c.members = c.ws.Members()
	if err := c.selectReleasable(); err != nil {
		return nil, err
	}
	return c, nil
}

// selectReleasable decides the member and releasable the run answers for.
func (c *Context) selectReleasable() error {
	here, err := c.ws.MemberAtDirectory(c.in.Dir)
	if err != nil {
		return err
	}
	if c.in.Releasable != "" {
		r, ok := c.ws.Declarations.Releasable(c.in.Releasable)
		if !ok {
			return fmt.Errorf("no releasable is named %q; the releasables %s declares are %s", c.in.Releasable, declarations.ReleasablesFile, strings.Join(c.releasableNames(), ", "))
		}
		if here.Versioned() && here.Releasable != r.Name {
			return fmt.Errorf("the checks were asked to answer for the releasable %q from %s, which lies in the member %q of the releasable %q; run them from a directory of %q, or from the repository root", r.Name, c.in.Dir, here.Name, here.Releasable, r.Name)
		}
		c.member = c.ws.MembersOf(r.Name)[0]
		c.releasable = &r
		return nil
	}
	c.member = here
	if c.ws.IsWorkspace() && c.atRoot() {
		c.unselected = c.releasableNames()
		if c.unselected == nil {
			c.unselected = []string{}
		}
		return nil
	}
	if r, ok := c.ws.ReleasableOf(here); ok {
		c.releasable = &r
	}
	return nil
}

// atRoot reports whether the run stands at the repository root itself.
func (c *Context) atRoot() bool {
	rel, err := c.ws.RelativePath(c.in.Dir)
	return err == nil && rel == "."
}

func (c *Context) releasableNames() []string {
	var names []string
	for _, r := range c.ws.Releasables() {
		names = append(names, r.Name)
	}
	sort.Strings(names)
	return names
}

// ProjectRoot is the directory of the member the run answers for, or the
// repository root when the declarations cannot be read.
func (c *Context) ProjectRoot() string {
	if c.ws == nil {
		return c.root
	}
	return c.ws.MemberDir(c.member)
}

// Root is the repository root, absolute.
func (c *Context) Root() string { return c.root }

// Repo is the repository, bound to the run's effects handle.
func (c *Context) Repo() git.Repo { return c.repo }

// Effects is the run's effects handle.
func (c *Context) Effects() *strictcli.Effects { return c.e }

// Now is the date the run judges the lifecycle-and-license record on.
func (c *Context) Now() time.Time { return c.in.Now }

// Workspace is the repository's model. A check reaches it only once the
// declarations were read (prepare refuses otherwise).
func (c *Context) Workspace() *workspace.Workspace {
	if c.declErr != nil {
		panic(unanswered(c.declErr.Error()))
	}
	return c.ws
}

// Declarations are the repository's release declarations.
func (c *Context) Declarations() *declarations.Releasables { return c.Workspace().Declarations }

// Options are the repository's accepted options.
func (c *Context) Options() *options.Options {
	if c.optsErr != nil {
		panic(unanswered(c.optsErr.Error()))
	}
	return c.opts
}

// Record is the repository's lifecycle-and-license record; a record that
// cannot be read leaves the check unanswered, naming why.
func (c *Context) Record() *lifecycle.Record {
	if c.recErr != nil {
		panic(unanswered(fmt.Sprintf("the lifecycle-and-license record cannot be read, so this check cannot be answered (lifecycle-record-valid names the problem): %v", c.recErr)))
	}
	return c.record
}

// Member is the member the run answers for.
func (c *Context) Member() declarations.Member {
	c.Workspace()
	return c.member
}

// Releasable is the releasable the run answers for, and false for a member
// versioned under none.
func (c *Context) Releasable() (declarations.Releasable, bool) {
	c.Workspace()
	if c.releasable == nil {
		return declarations.Releasable{}, false
	}
	return *c.releasable, true
}

// Members are the members the check sees: every member, narrowed by the
// check's scope.
func (c *Context) Members() []declarations.Member {
	c.Workspace()
	return append([]declarations.Member(nil), c.members...)
}

// CheckTimeout bounds each program a check starts.
func (c *Context) CheckTimeout() time.Duration {
	if c.in.CheckTimeout > 0 {
		return c.in.CheckTimeout
	}
	if c.ws != nil && c.ws.Declarations.Timeouts.CheckSeconds > 0 {
		return time.Duration(c.ws.Declarations.Timeouts.CheckSeconds) * time.Second
	}
	return ShippedCheckTimeout
}

// PushRefs are the refs the push the pre-push hook runs for sends, and
// false outside a push.
func (c *Context) PushRefs() ([]git.PushedRef, bool, error) {
	if c.in.PushLines == nil {
		return nil, false, nil
	}
	refs, err := git.ParsePrePushLines(c.in.PushLines)
	if err != nil {
		return nil, true, err
	}
	return refs, true, nil
}

// Upstream is the URL of the repository this one is a fork of, and empty
// for a repository that is no fork.
func (c *Context) Upstream() string {
	if c.upstreamErr != nil {
		panic(unanswered(c.upstreamErr.Error()))
	}
	return c.upstreamURL
}

// UpstreamExclude are the revisions of the history a fork inherited, which
// every unreleased range and every push range leaves out.
func (c *Context) UpstreamExclude() []string {
	if c.upstreamErr != nil {
		panic(unanswered(c.upstreamErr.Error()))
	}
	return append([]string(nil), c.upstreamExclude...)
}

// Subject is the changelog of the releasable r.
func (c *Context) Subject(r declarations.Releasable) (changelog.Subject, error) {
	return changelog.NewSubject(c.repo, c.Workspace(), r.Name, c.Upstream(), c.UpstreamExclude())
}

// GitHub is the gh client, bound to the run's effects handle.
func (c *Context) GitHub() github.Client {
	client, err := github.New(c.e)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return client
}

// noGitHubRepository is the skip reason of a networked check in a
// repository that names no GitHub repository.
const noGitHubRepository = "no GitHub repository is declared (github_repository in .strictmetadata/releasables/releasables.toml) or named by the origin remote, so there is no repository to ask"

// GitHubRepository is the repository the project releases from: the
// declared github_repository, or the one the origin remote's URL names.
// found is false when there is no declaration and origin is absent or names
// no GitHub repository; a declaration that does not parse is an error.
func (c *Context) GitHubRepository() (repo github.Repository, found bool, err error) {
	declared := c.Declarations().GitHubRepository
	if declared != "" {
		repo, err := github.ParseRepository(declared)
		if err != nil {
			return github.Repository{}, false, err
		}
		return repo, true, nil
	}
	configured, err := c.repo.RemoteConfigured("origin")
	if err != nil || !configured {
		return github.Repository{}, false, err
	}
	url, err := c.repo.RemoteURL("origin")
	if err != nil {
		return github.Repository{}, false, err
	}
	repo, err = github.RepositoryFromRemoteURL(url)
	if err != nil {
		return github.Repository{}, false, nil
	}
	return repo, true, nil
}

// Registry is the registry client, sending every request as a declared read
// through the run's effects handle.
func (c *Context) Registry() registry.Client {
	client, err := registry.New(registry.Reads(c.e))
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return client
}

// Home is the user's home directory, refused when the caller stated none.
func (c *Context) Home() string {
	if c.in.Home == "" {
		panic(unanswered("no home directory was given to read ~/.npmrc from"))
	}
	return c.in.Home
}

// UVProjectEnvironment is UV_PROJECT_ENVIRONMENT as the caller read it.
func (c *Context) UVProjectEnvironment() string { return c.in.UVProjectEnvironment }

// IndexPath is the confidential-name index file, refused when the caller
// stated none.
func (c *Context) IndexPath() string {
	if c.in.IndexPath == "" {
		panic(unanswered("no confidential-name index was given to scan with"))
	}
	return c.in.IndexPath
}
