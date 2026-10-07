package historyrewrite

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/semver"
)

// SafegitMinimum is the safegit release rlsbl's history rewrites (the
// scrub, and the declassification's squash) are built against: the release
// that ships with this rlsbl, whose rewrites journal every commit map, remap
// changelog ids at every rewritten commit (--remap-shas-in), report their
// cleanup, whose `scrub file` states its mode (--delete or --replace-with)
// instead of inferring it, and which has `scrub squash`.
var SafegitMinimum = semver.Version{Major: 0, Minor: 31, Patch: 0}

// SafegitInterfaceVersion is the version of strictcli's machine-mode
// document that safegit prints under --json at SafegitMinimum. Any other
// version is refused by name: reading another document shape as this one
// would build a rewrite state missing what the rewrite needs.
const SafegitInterfaceVersion = 3

// safegitVersionTimeout bounds `safegit --version`.
const safegitVersionTimeout = 15 * time.Second

// safegitScrubTimeout bounds one safegit rewrite.
const safegitScrubTimeout = 10 * time.Minute

var releaseVersion = regexp.MustCompile(`(\d+)\.(\d+)\.(\d+)`)

// requireSafegit refuses a safegit that is missing or older than
// SafegitMinimum. `safegit --version` is on the observe allowlist, so the
// check runs under --dry-run too.
func requireSafegit(e *strictcli.Effects) error {
	install := fmt.Sprintf("`rlsbl release scrub` rewrites history through safegit %s or newer; install that safegit and run the scrub again", SafegitMinimum)
	done, err := e.Run([]interface{}{"safegit", "--version"}, strictcli.Check(false), strictcli.Timeout(safegitVersionTimeout))
	if err != nil {
		return fmt.Errorf("safegit could not be run (%v): %s", err, install)
	}
	if done.ExitCode() != 0 {
		return fmt.Errorf("`safegit --version` exited %d (%s): %s", done.ExitCode(), strings.TrimSpace(done.Stderr()), install)
	}
	printed := strings.TrimSpace(done.Stdout())
	fields := strings.Fields(printed)
	var m []string
	if len(fields) > 0 {
		m = releaseVersion.FindStringSubmatch(fields[len(fields)-1])
	}
	if m == nil {
		return fmt.Errorf("the safegit version cannot be read from %q: %s", printed, install)
	}
	found, err := semver.Parse(m[1] + "." + m[2] + "." + m[3])
	if err != nil {
		return fmt.Errorf("the safegit version cannot be read from %q (%v): %s", printed, err, install)
	}
	if semver.Compare(found, SafegitMinimum) < 0 {
		return fmt.Errorf("safegit %s is installed, and %s", found, install)
	}
	return nil
}

// TagRewrite is one tag a rewrite moved, as safegit reports it.
type TagRewrite struct {
	Refname   string `json:"refname"`
	OldSHA    string `json:"old_sha"`
	NewSHA    string `json:"new_sha"`
	Annotated bool   `json:"annotated"`
}

// scrubPayload is what the scrub reads of safegit's scrub payload; every
// other member is safegit's own and passed over.
type scrubPayload struct {
	Rewrites          map[string]string `json:"rewrites"`
	Tags              []TagRewrite      `json:"tags"`
	CommitsRewritten  *int              `json:"commits_rewritten"`
	OldHead           string            `json:"old_head"`
	NewHead           string            `json:"new_head"`
	PreRewriteRemotes map[string]string `json:"pre_rewrite_remotes"`
	CleanupOK         *bool             `json:"cleanup_ok"`
	CleanupErrors     []string          `json:"cleanup_errors"`
}

type machineDocument struct {
	InterfaceVersion *int            `json:"interface_version"`
	Payload          json.RawMessage `json:"payload"`
}

// parseMachineDocument reads safegit's --json stdout, which is one document:
// strictcli's machine-mode document. It returns the payload, nil when the
// command emitted none (a scrub that found nothing to rewrite). Anything but
// a machine-mode document of SafegitInterfaceVersion is refused, naming the safegit the
// scrub needs.
func parseMachineDocument(stdout string) (*scrubPayload, error) {
	text := strings.TrimSpace(stdout)
	need := fmt.Sprintf("the scrub needs safegit %s or newer, whose --json prints strictcli's machine-mode document at interface_version %d", SafegitMinimum, SafegitInterfaceVersion)
	if text == "" {
		return nil, fmt.Errorf("safegit --json printed nothing; %s", need)
	}
	var env machineDocument
	if err := json.Unmarshal([]byte(text), &env); err != nil {
		return nil, fmt.Errorf("safegit --json printed something that is not one JSON document (%v); %s", err, need)
	}
	if env.InterfaceVersion == nil {
		return nil, fmt.Errorf("safegit --json printed a document that is not strictcli's machine-mode document (it has no interface_version); %s", need)
	}
	if *env.InterfaceVersion != SafegitInterfaceVersion {
		return nil, fmt.Errorf("safegit's machine-mode document declares interface_version %d, which this rlsbl does not read; %s", *env.InterfaceVersion, need)
	}
	if len(env.Payload) == 0 || string(env.Payload) == "null" {
		return nil, nil
	}
	var p scrubPayload
	if err := json.Unmarshal(env.Payload, &p); err != nil {
		return nil, fmt.Errorf("safegit's scrub payload cannot be read: %v", err)
	}
	return &p, nil
}

// rewritten reports whether the payload records a rewrite.
func (p *scrubPayload) rewritten() bool { return p != nil && len(p.Rewrites) > 0 }

// ScrubMode is which of safegit's scrub commands the scrub runs.
type ScrubMode string

// The scrub modes.
const (
	// ModePattern rewrites every match of a regular expression.
	ModePattern ScrubMode = "pattern"
	// ModeFile rewrites one file throughout the range.
	ModeFile ScrubMode = "file"
	// ModeRecipe runs a recipe of rewrites in one pass.
	ModeRecipe ScrubMode = "recipe"
)

// ScrubRequest is one `release scrub`.
type ScrubRequest struct {
	Mode ScrubMode
	// Pattern, with Replace or Mangle, is the pattern mode's.
	Pattern string
	Replace string
	Mangle  bool
	// File is the file mode's repository-relative path.
	File string
	// Recipe is the recipe mode's path, relative to the working directory or
	// absolute.
	Recipe string
	// FromCommit is the first commit rewritten; empty with EntireHistory.
	FromCommit    string
	EntireHistory bool
	Reason        string
	// IndexPath is the machine-local confidential-name index the rewritten
	// Release bodies are scanned against.
	IndexPath string
}

// validate refuses a request no scrub can run, before anything is read.
func (r ScrubRequest) validate() error {
	if strings.TrimSpace(r.Reason) == "" {
		return fmt.Errorf("--reason must not be empty: it is the audit trail of an irreversible rewrite, recorded in the scrub commit and its archive")
	}
	if r.EntireHistory == (r.FromCommit != "") {
		return fmt.Errorf("the commit range is --from-commit <commit> or --entire-history, one of them")
	}
	switch r.Mode {
	case ModePattern:
		if r.Pattern == "" {
			return fmt.Errorf("--pattern must not be empty: an empty regular expression is not a scrub")
		}
		if r.Mangle == (r.Replace != "") {
			return fmt.Errorf("the pattern mode replaces each match with --replace <text> or with random text under --mangle, one of them")
		}
	case ModeFile:
		if r.File == "" {
			return fmt.Errorf("--file must not be empty")
		}
		if filepath.IsAbs(r.File) || strings.HasPrefix(filepath.ToSlash(filepath.Clean(r.File)), "../") {
			return fmt.Errorf("--file %q must be a path inside the repository, relative to its root", r.File)
		}
	case ModeRecipe:
		if r.Recipe == "" {
			return fmt.Errorf("--recipe must not be empty")
		}
	default:
		return fmt.Errorf("the scrub mode is pattern, file, or recipe, not %q", r.Mode)
	}
	return nil
}

// safegitArgs is the safegit argv (after "safegit") the request runs, with
// --remap-shas-in for every glob, so safegit rewrites the changelog's commit
// ids at every rewritten commit and every version of the changelog stays
// consistent. --approve-consequential is stated before the command words:
// running the scrub is the consent, and safegit's own prompt is not one
// --json answers. root is the repository root, against which a file mode's
// replacement copy is resolved; fileExists says whether that copy is on
// disk. dryRun adds safegit's --dry-run, so the recorded invocation is the
// one that shows safegit's own counts.
func (r ScrubRequest) safegitArgs(root string, globs []string, fileExists, dryRun bool) []string {
	args := []string{"--approve-consequential", "scrub"}
	switch r.Mode {
	case ModePattern:
		args = append(args, "match", "--json")
	case ModeFile:
		args = append(args, "file", "--json")
	case ModeRecipe:
		args = append(args, "run", "--json")
	}
	if dryRun {
		args = append(args, "--dry-run")
	}
	switch r.Mode {
	case ModePattern:
		args = append(args, "--pattern", r.Pattern)
		if r.Mangle {
			args = append(args, "--mangle")
		} else {
			args = append(args, "--replace", r.Replace)
		}
	case ModeFile:
		// The file mode replaces every past version of the file with the copy
		// on disk now, or removes it from every commit when there is no copy
		// on disk; safegit is told which, never left to infer it.
		if fileExists {
			args = append(args, "--replace-with", filepath.Join(root, filepath.FromSlash(r.File)))
		} else {
			args = append(args, "--delete")
		}
	case ModeRecipe:
		args = append(args, r.Recipe)
	}
	if r.EntireHistory {
		args = append(args, "--entire-history")
	} else {
		args = append(args, "--from", r.FromCommit)
	}
	for _, g := range globs {
		args = append(args, "--remap-shas-in", g)
	}
	args = append(args, "--reason", r.Reason)
	if r.Mode == ModeFile {
		args = append(args, r.File)
	}
	return args
}

// argv is args as the effects handle's argv for safegit.
func argv(program string, args []string) []interface{} {
	out := make([]interface{}, 0, len(args)+1)
	out = append(out, program)
	for _, a := range args {
		out = append(out, a)
	}
	return out
}
