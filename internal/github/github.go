// Package github is rlsbl's GitHub surface: every call goes through the gh
// command line, started through the strictcli effects handle, so gh resolves
// and applies the credential inside its own process and rlsbl never holds a
// token. Reads use argvs the observe allowlist admits (`gh api --method GET`,
// `gh release view`, `gh release list`, `gh run list`, and the like), so they
// run for real under --dry-run; writes are recorded instead of performed.
//
// Every call names its repository explicitly (`--repo owner/name`, or a
// `repos/owner/name/...` API path): nothing here relies on the directory gh
// happens to run in or on an environment variable choosing the repository.
//
// A question gh cannot answer is an error naming the command and what gh
// printed; nothing here turns a failed read into "no", "absent", or an empty
// list. Where GitHub's answer has a state of its own (a Release or a secret
// that does not exist), the result says so.
package github

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// The bounds of gh's work. They are backstops against a hung process, not
// budgets.
const (
	readTimeout    = 2 * time.Minute
	listTimeout    = 2 * time.Minute
	writeTimeout   = 2 * time.Minute
	probeTimeout   = 30 * time.Second
	versionTimeout = 15 * time.Second
)

// Runner starts programs: the strictcli effects handle, or the screened view
// of it a reconciler's observation gets (previewapply.Observer), which
// refuses every gh command that writes.
type Runner interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
}

// Client runs gh through a runner.
type Client struct {
	r Runner
}

// New binds a client to the runner every gh command goes through.
func New(r Runner) (Client, error) {
	if r == nil {
		return Client{}, errors.New("github: no effects handle to run gh through")
	}
	return Client{r: r}, nil
}

// Repository is a GitHub repository, owner and name.
type Repository struct {
	Owner string
	Name  string
}

// String is the repository's owner/name slug.
func (r Repository) String() string { return r.Owner + "/" + r.Name }

// apiPath is the repository's REST path, repos/owner/name, followed by rest.
func (r Repository) apiPath(rest string) string {
	p := "repos/" + r.Owner + "/" + r.Name
	if rest != "" {
		p += "/" + rest
	}
	return p
}

var slugPart = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

// ParseRepository reads an owner/name slug. Anything else (more or fewer
// parts, an empty part, a character GitHub does not allow in a name) is
// refused, naming the slug.
func ParseRepository(slug string) (Repository, error) {
	owner, name, ok := strings.Cut(slug, "/")
	if !ok || !slugPart.MatchString(owner) || !slugPart.MatchString(name) || name == "." || name == ".." {
		return Repository{}, fmt.Errorf("%q is not a GitHub repository slug; write it as owner/name", slug)
	}
	return Repository{Owner: owner, Name: name}, nil
}

var (
	httpsRemote = regexp.MustCompile(`^https?://([^/]+)/(.+)$`)
	scpRemote   = regexp.MustCompile(`^(?:[^@/:]+@)?([^@/:]+):(.+/.+)$`)
)

// githubHost is the one host whose repositories a remote URL may name.
const githubHost = "github.com"

// RepositoryFromRemoteURL reads the repository a git remote URL names:
// https://github.com/owner/name[.git], or the SCP form
// [user@]host:owner/name[.git] whose host is github.com or an SSH alias (a
// host without a dot, which the SSH configuration resolves). Any other host
// names another forge or a server rlsbl cannot ask GitHub about, and is
// refused, as is a URL in any other form, naming it.
func RepositoryFromRemoteURL(url string) (Repository, error) {
	refuse := fmt.Errorf("the remote URL %q names no GitHub repository (expected https://github.com/owner/name, git@github.com:owner/name, or an SSH alias without a dot such as gh:owner/name)", url)
	if m := httpsRemote.FindStringSubmatch(url); m != nil {
		if m[1] != githubHost {
			return Repository{}, refuse
		}
		repo, err := ParseRepository(strings.TrimSuffix(strings.TrimRight(m[2], "/"), ".git"))
		if err != nil {
			return Repository{}, refuse
		}
		return repo, nil
	}
	if m := scpRemote.FindStringSubmatch(url); m != nil {
		if m[1] != githubHost && strings.Contains(m[1], ".") {
			return Repository{}, refuse
		}
		repo, err := ParseRepository(strings.TrimSuffix(m[2], ".git"))
		if err != nil {
			return Repository{}, refuse
		}
		return repo, nil
	}
	return Repository{}, refuse
}

// ResolveRepository is the repository a project releases from: the declared
// github_repository when the declarations state one, otherwise the one the
// origin remote's URL names. Both empty is refused.
func ResolveRepository(declared, originURL string) (Repository, error) {
	if declared != "" {
		return ParseRepository(declared)
	}
	if originURL == "" {
		return Repository{}, errors.New("no GitHub repository is declared (github_repository) and this checkout has no origin remote to read one from")
	}
	return RepositoryFromRemoteURL(originURL)
}

// result is what one gh read printed and exited with.
type result struct {
	stdout string
	stderr string
	code   int
}

// read runs a gh command whose output is read, and returns its output
// whatever it exited with. Only a gh that could not be run is an error.
func (c Client) read(timeout time.Duration, args ...string) (result, error) {
	done, err := c.r.Run(ghArgv(args), strictcli.Check(false), strictcli.Timeout(timeout))
	if err != nil {
		return result{}, fmt.Errorf("gh %s: %w", strings.Join(args, " "), err)
	}
	return result{stdout: done.Stdout(), stderr: done.Stderr(), code: done.ExitCode()}, nil
}

// output runs a gh read that must succeed and returns its stdout.
func (c Client) output(timeout time.Duration, args ...string) (string, error) {
	res, err := c.read(timeout, args...)
	if err != nil {
		return "", err
	}
	if res.code != 0 {
		return "", failed(args, res)
	}
	return res.stdout, nil
}

// write runs a gh command that changes something on GitHub. Its output
// streams to the command's own streams, so what gh says about a failure
// reaches the operator, and a non-zero exit is an error. Under --dry-run it
// is recorded rather than run. stdin, when not nil, is handed to gh on its
// standard input and never shown anywhere the framework renders the effect.
func (c Client) write(stdin []byte, extra []strictcli.EffectOption, args ...string) error {
	opts := []strictcli.EffectOption{strictcli.Timeout(writeTimeout), strictcli.Stream(true)}
	if stdin != nil {
		opts = append(opts, strictcli.Stdin(stdin))
	}
	opts = append(opts, extra...)
	if _, err := c.r.Run(ghArgv(args), opts...); err != nil {
		return fmt.Errorf("gh %s: %w", strings.Join(args, " "), err)
	}
	return nil
}

// failed is the error for a gh read that exited non-zero.
func failed(args []string, res result) error {
	return fmt.Errorf("gh %s exited %d: %s", strings.Join(args, " "), res.code, detail(res))
}

// detail is the first line of what gh printed about a failure.
func detail(res result) string {
	text := strings.TrimSpace(res.stderr)
	if text == "" {
		text = strings.TrimSpace(res.stdout)
	}
	if text == "" {
		return "gh printed nothing"
	}
	first, _, _ := strings.Cut(text, "\n")
	return first
}

// ghArgv is the effects handle's argv for gh args.
func ghArgv(args []string) []interface{} {
	out := make([]interface{}, 0, len(args)+1)
	out = append(out, "gh")
	for _, a := range args {
		out = append(out, a)
	}
	return out
}

// lines splits output into its non-empty lines.
func lines(s string) []string {
	var out []string
	for _, line := range strings.Split(s, "\n") {
		if line = strings.TrimRight(line, "\r"); strings.TrimSpace(line) != "" {
			out = append(out, line)
		}
	}
	return out
}

// CheckInstalled refuses when gh cannot be run.
func (c Client) CheckInstalled() error {
	res, err := c.read(versionTimeout, "--version")
	if err != nil {
		return fmt.Errorf("the gh command line could not be run (install it from https://cli.github.com): %w", err)
	}
	if res.code != 0 {
		return fmt.Errorf("`gh --version` exited %d: %s", res.code, detail(res))
	}
	return nil
}

// CheckAuth refuses when gh holds no valid credential for github.com, naming
// `gh auth login`. The host is named explicitly: the observe allowlist pins
// this argv, because the bare `gh auth status` also admits the forms that
// print the credential.
func (c Client) CheckAuth() error {
	res, err := c.read(probeTimeout, "auth", "status", "--hostname", "github.com")
	if err != nil {
		return fmt.Errorf("the gh command line could not be run (install it from https://cli.github.com): %w", err)
	}
	if res.code != 0 {
		return fmt.Errorf("gh is not authenticated for github.com (%s); run `gh auth login`, or set GH_TOKEN", detail(res))
	}
	return nil
}

// APIGet reads one GitHub REST path with `gh api --method GET`. With
// paginate gh follows the Link headers itself; jq, when not empty, is gh's
// own projection of each page. The path must not name a repository through
// gh's {owner}/{repo} placeholders: they resolve from whatever directory gh
// runs in.
func (c Client) APIGet(path string, paginate bool, jq string) (string, error) {
	if strings.Contains(path, "{owner}") || strings.Contains(path, "{repo}") {
		return "", fmt.Errorf("the API path %q uses gh's {owner}/{repo} placeholders; name the repository in the path", path)
	}
	args := []string{"api", "--method", "GET"}
	if paginate {
		args = append(args, "--paginate")
	}
	args = append(args, path)
	if jq != "" {
		args = append(args, "--jq", jq)
	}
	return c.output(readTimeout, args...)
}
