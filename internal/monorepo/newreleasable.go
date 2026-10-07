package monorepo

import (
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// licenseProblem refuses a --license value that is neither an SPDX
// identifier nor proprietary, as `transition license` refuses one.
func licenseProblem(license string) error {
	if license == "" || strings.TrimSpace(license) != license || strings.ContainsAny(license, " \t\n") {
		return fmt.Errorf("--license %q is not a license; name an SPDX identifier (MIT, Apache-2.0, ...) or %s", license, lifecycle.ProprietaryLicense)
	}
	return nil
}

// requireLicenseForCreated is the refusal of a --license that does not fit
// the command: one is required when the command creates a releasable, and
// refused when it creates none, since no license period would be opened
// for it.
func requireLicenseForCreated(creates bool, license, joined string) error {
	switch {
	case creates && license == "":
		return fmt.Errorf("the releasable this command creates needs its license stated: pass --license <SPDX identifier or %s>. A license is never derived, and it opens the releasable's license period in %s", lifecycle.ProprietaryLicense, lifecycle.RecordFile)
	case !creates && license != "":
		return fmt.Errorf("--license states the license of the releasable this command creates, and it creates none (%s); drop --license. `rlsbl transition license` changes the license of a declared releasable", joined)
	case creates:
		return licenseProblem(license)
	}
	return nil
}

// requirePrivateForProprietary refuses a proprietary license unless GitHub
// reports the repository private, as `transition classify` requires before
// it makes a releasable proprietary: a confidential repository must be
// private, and making it private is the owner's action outside rlsbl.
func requirePrivateForProprietary(license string, gh github.Client, repo git.Repo, declared string) error {
	if license != lifecycle.ProprietaryLicense {
		return nil
	}
	url := ""
	configured, err := repo.RemoteConfigured("origin")
	if err != nil {
		return err
	}
	if configured {
		if url, err = repo.RemoteURL("origin"); err != nil {
			return err
		}
	}
	slug, err := github.ResolveRepository(declared, url)
	if err != nil {
		return fmt.Errorf("--license %s makes the repository confidential, which requires GitHub to report it private, and the repository cannot be named: %w", lifecycle.ProprietaryLicense, err)
	}
	visibility, askErr := publishrules.GitHubVisibility(gh, slug).Visibility()
	switch visibility {
	case lifecycle.VisibilityPrivate:
		return nil
	case lifecycle.VisibilityPublic:
		return fmt.Errorf("GitHub reports %s public, and a proprietary releasable requires a private repository; nothing was written. Making the repository private is the owner's decision, outside rlsbl (`gh repo edit %s --visibility private --accept-visibility-change-consequences`, or the repository's settings); then run this again", slug, slug)
	}
	return fmt.Errorf("the visibility of %s on GitHub could not be had (%v), and a proprietary releasable is created only in a repository GitHub reports private; nothing was written", slug, askErr)
}

// recordCreatedReleasable gives the releasable r a command creates its
// entries in rec from the date of on: an active lifecycle period, a
// license period under license, and its releasable-name identity owning
// its tag namespace. An open entry rec holds already (an absorb's arriving
// entries, or an earlier run of the same command) is kept when it agrees: a
// lifecycle period of any status, and a license period of the same
// license; one of another license is refused. An open releasable-name
// identity of another name is closed on that date and the releasable's own
// opened.
func recordCreatedReleasable(rec *lifecycle.Record, r declarations.Releasable, license string, on time.Time, reason string) error {
	lifecycleOpen := false
	for _, l := range rec.Lifecycle() {
		lifecycleOpen = lifecycleOpen || (l.Subject == r.Name && l.Open())
	}
	if !lifecycleOpen {
		if err := rec.OpenPeriod(lifecycle.TableLifecycle, r.Name, string(lifecycle.StatusActive), on, reason); err != nil {
			return err
		}
	}
	licenseOpen := false
	for _, l := range rec.Licenses() {
		if l.Subject != r.Name || !l.Open() {
			continue
		}
		if l.License != license {
			return fmt.Errorf("%s licenses %q %s since %s, and --license says %s; pass --license %s, or change the license afterwards with `rlsbl transition license`", lifecycle.RecordFile, r.Name, l.License, l.From.Format(time.DateOnly), license, l.License)
		}
		licenseOpen = true
	}
	if !licenseOpen {
		if err := rec.OpenPeriod(lifecycle.TableLicenses, r.Name, license, on, reason); err != nil {
			return err
		}
	}
	scheme, err := workspace.SchemeOf(r)
	if err != nil {
		return err
	}
	for _, id := range rec.Identities() {
		if id.Subject != r.Name || id.Facet != lifecycle.FacetReleasableName || id.Pending() || !id.Open() {
			continue
		}
		if id.Value == r.Name {
			return nil
		}
		if err := rec.CloseIdentity(r.Name, lifecycle.FacetReleasableName, on); err != nil {
			return err
		}
	}
	return rec.AddIdentity(lifecycle.Identity{
		Subject:     r.Name,
		Facet:       lifecycle.FacetReleasableName,
		Value:       r.Name,
		TagPatterns: []string{scheme.ListGlob()},
		Period:      lifecycle.Period{From: on},
		Reason:      reason,
	})
}
