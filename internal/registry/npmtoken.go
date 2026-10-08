package registry

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/github"
)

// NpmrcTokenKey is the ~/.npmrc key npm login writes the registry token
// under.
const NpmrcTokenKey = "//registry.npmjs.org/:_authToken"

// NpmTokenSecret is the Actions secret CI publishes to npm with.
const NpmTokenSecret = "NPM_TOKEN"

// SyncCommand brings repositories' NPM_TOKEN secrets up to the local token.
const SyncCommand = "rlsbl secrets sync-npm-token"

// tokenListTimeout bounds `npm token list --json`.
const tokenListTimeout = time.Minute

// NpmrcPath is the developer's own ~/.npmrc under home.
func NpmrcPath(home string) string {
	return filepath.Join(home, ".npmrc")
}

// ReadNpmrcToken is the npm registry token the npm config file at path
// holds. Only one literal `//registry.npmjs.org/:_authToken=<token>` line
// counts: none, several, an empty value, or a value naming an environment
// variable (${NPM_TOKEN}, which is not a token this machine holds) is
// refused.
func ReadNpmrcToken(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("cannot read %s: %w", path, err)
	}
	var values []string
	for _, line := range strings.Split(string(data), "\n") {
		key, value, ok := strings.Cut(strings.TrimSpace(line), "=")
		if ok && strings.TrimSpace(key) == NpmrcTokenKey {
			values = append(values, strings.TrimSpace(value))
		}
	}
	switch {
	case len(values) == 0 || values[len(values)-1] == "":
		return "", fmt.Errorf("%s holds no `%s=<token>` line, so this machine has no npm token to check or copy. Log in with `npm login` first", path, NpmrcTokenKey)
	case len(values) > 1:
		return "", fmt.Errorf("%s holds %d `%s=` lines; npm reads one token per registry. Remove all but the current one", path, len(values), NpmrcTokenKey)
	case strings.HasPrefix(values[0], "${"):
		return "", fmt.Errorf("%s names an environment variable in its `%s=` line instead of holding a token; there is no token on this machine to check or copy", path, NpmrcTokenKey)
	}
	return values[0], nil
}

// NpmWhoami is the npm user token authenticates as, asked of the registry
// with the token as a bearer credential. A 401 or 403 is npm refusing the
// token; anything else that yields no user is a question left unanswered.
func (c Client) NpmWhoami(token string) (string, error) {
	r, err := c.get(npmWhoamiURL, strictcli.Header("Authorization", "Bearer "+token), strictcli.Redact(token))
	if err != nil {
		return "", fmt.Errorf("could not ask npm whether it accepts the token in ~/.npmrc (%w)", err)
	}
	switch {
	case r.status == 401 || r.status == 403:
		return "", fmt.Errorf("npm does not accept the token in ~/.npmrc (HTTP %d from %s): it expired or was revoked. Log in again with `npm login`, then run `%s`", r.status, npmWhoamiURL, SyncCommand)
	case r.status != 200:
		return "", fmt.Errorf("could not ask npm whether it accepts the token in ~/.npmrc (HTTP %d from %s)", r.status, npmWhoamiURL)
	}
	var doc struct {
		Username string `json:"username"`
	}
	if err := json.Unmarshal(r.body, &doc); err != nil || doc.Username == "" {
		return "", fmt.Errorf("%s answered without a username, so whether npm accepts the token in ~/.npmrc is unknown", npmWhoamiURL)
	}
	return doc.Username, nil
}

// Runner starts programs: the strictcli effects handle.
type Runner interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
}

// listingMatches reports whether the token npm lists as shown (its first
// and last characters around "...") is token. A display of any other shape
// matches nothing, because matching it would be a guess.
func listingMatches(token, shown string) bool {
	prefix, suffix, ok := strings.Cut(shown, "...")
	return ok && prefix != "" && strings.HasPrefix(token, prefix) && strings.HasSuffix(token, suffix)
}

// ParseTimestamp reads an RFC 3339 timestamp with a zone, as npm and
// GitHub write them; ok is false for anything else.
func ParseTimestamp(s string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339Nano, s)
	return t, err == nil
}

// NpmTokenCreatedAt is when npm created token, read from `npm token list
// --json`: one unrevoked listed token must match it. No match, several, an
// unreadable listing, or a creation time that does not parse is an error.
func NpmTokenCreatedAt(r Runner, token string) (time.Time, error) {
	done, err := r.Run([]interface{}{"npm", "token", "list", "--json"}, strictcli.Check(false), strictcli.Timeout(tokenListTimeout))
	if err != nil {
		return time.Time{}, fmt.Errorf("`npm token list --json` failed to run: %w", err)
	}
	if done.ExitCode() != 0 {
		first, _, _ := strings.Cut(strings.TrimSpace(done.Stderr()), "\n")
		if first != "" {
			first = ": " + first
		}
		return time.Time{}, fmt.Errorf("`npm token list --json` exited %d%s", done.ExitCode(), first)
	}
	var raw json.RawMessage
	if err := json.Unmarshal([]byte(done.Stdout()), &raw); err != nil {
		return time.Time{}, fmt.Errorf("`npm token list --json` printed something that is not JSON (%w)", err)
	}
	// Revoked is a boolean in older listings and the revocation time (null
	// for a live token) in npm 10's.
	type entry struct {
		Token   string          `json:"token"`
		Revoked json.RawMessage `json:"revoked"`
		Created string          `json:"created"`
	}
	revoked := func(e entry) bool {
		v := strings.TrimSpace(string(e.Revoked))
		return v != "" && v != "null" && v != "false"
	}
	var listed []entry
	if err := json.Unmarshal(raw, &listed); err != nil {
		var wrapped struct {
			Objects *[]entry `json:"objects"`
		}
		if err := json.Unmarshal(raw, &wrapped); err != nil || wrapped.Objects == nil {
			return time.Time{}, errors.New("`npm token list --json` did not print a list of tokens")
		}
		listed = *wrapped.Objects
	}
	var matches []entry
	for _, e := range listed {
		if !revoked(e) && listingMatches(token, e.Token) {
			matches = append(matches, e)
		}
	}
	if len(matches) != 1 {
		return time.Time{}, fmt.Errorf("`npm token list --json` shows %d unrevoked tokens matching the one in ~/.npmrc, where one and only one is needed to know when it was created", len(matches))
	}
	created, ok := ParseTimestamp(matches[0].Created)
	if !ok {
		return time.Time{}, errors.New("`npm token list --json` gives the token in ~/.npmrc no creation time that parses")
	}
	return created, nil
}

// SyncVerdict is what the npm-token-synced comparison found for one
// repository: problems are errors, notes say what passed.
type SyncVerdict struct {
	Problems []string
	Notes    []string
}

// OK reports whether the verdict found no problem.
func (v SyncVerdict) OK() bool { return len(v.Problems) == 0 }

// CompareNpmTokenSync judges whether repo's NPM_TOKEN secret carries the
// local token npm accepts as user and created at created: a secret last
// set before the token was created holds an older one. An absent secret,
// or one without an update time that parses, is a problem too, never a
// pass.
func CompareNpmTokenSync(repo github.Repository, user string, created time.Time, secret github.Secret) SyncVerdict {
	if !secret.Present {
		return SyncVerdict{Problems: []string{fmt.Sprintf("%s has no %s secret, so its CI npm publish has no token at all (`%s` only replaces an existing one; ci-publish-secrets names the command that creates it)", repo, NpmTokenSecret, SyncCommand)}}
	}
	updated, ok := ParseTimestamp(secret.UpdatedAt)
	if !ok {
		return SyncVerdict{Problems: []string{fmt.Sprintf("%s: the GitHub API gives the %s secret no update time that parses, so whether it holds the current npm token is unknown. Run `%s` to set it", repo, NpmTokenSecret, SyncCommand)}}
	}
	if updated.Before(created) {
		return SyncVerdict{Problems: []string{fmt.Sprintf("%s's %s secret was last set at %s, before the npm token in ~/.npmrc (user %s) was created at %s: CI would publish with an older token, which npm refuses once it expires. Run `%s` to set it from ~/.npmrc",
			repo, NpmTokenSecret, updated.Format(time.RFC3339), user, created.Format(time.RFC3339), SyncCommand)}}
	}
	return SyncVerdict{Notes: []string{fmt.Sprintf("npm accepts the token in ~/.npmrc (user %s), and %s's %s secret was set after it was created", user, repo, NpmTokenSecret)}}
}

// EvaluateNpmTokenSync asks whether the npm token in the config file at
// npmrc is live and whether repo's NPM_TOKEN secret was set after it was
// created: it reads the token, asks npm who it authenticates (whoami), asks
// npm when it was created (`npm token list --json` through r), reads the
// secret through gh, and compares. Every question left unanswered is a
// problem, never a pass. Whether a project publishes to npm from CI at all
// is the caller's question.
func (c Client) EvaluateNpmTokenSync(r Runner, gh github.Client, npmrc string, repo github.Repository) SyncVerdict {
	unanswered := func(err error) SyncVerdict {
		return SyncVerdict{Problems: []string{err.Error() + ". Until then CI cannot publish to npm."}}
	}
	token, err := ReadNpmrcToken(npmrc)
	if err != nil {
		return unanswered(err)
	}
	user, err := c.NpmWhoami(token)
	if err != nil {
		return unanswered(err)
	}
	created, err := NpmTokenCreatedAt(r, token)
	if err != nil {
		return unanswered(err)
	}
	secret, err := gh.Secret(repo, NpmTokenSecret)
	if err != nil {
		return SyncVerdict{Problems: []string{fmt.Sprintf("%s: could not read the %s secret (%v), so whether it holds the current npm token is unknown. Fix the credential or the connection and re-run, or run `%s` to set it", repo, NpmTokenSecret, err, SyncCommand)}}
	}
	return CompareNpmTokenSync(repo, user, created, secret)
}
