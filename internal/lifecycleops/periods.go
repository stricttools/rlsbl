package lifecycleops

import (
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
)

// PeriodRequest is a `transition lifecycle` or `transition license`: the
// subject, the value its new period holds (a status or a license), and why.
type PeriodRequest struct {
	// Dir is the working directory, absolute.
	Dir     string
	Subject string
	Value   string
	Reason  string
}

// Lifecycle closes the subject's lifecycle period on the command's date and
// opens one with the new status. The subject is a releasable or member the
// declarations declare, or one the record already holds a lifecycle period
// of (a subject retired after it left the declarations).
func (inv Invocation) Lifecycle(req PeriodRequest) error {
	m, err := inv.openManaged(req.Dir)
	if err != nil {
		return err
	}
	status := lifecycle.Status(req.Value)
	switch status {
	case lifecycle.StatusActive, lifecycle.StatusOnHold, lifecycle.StatusRetired:
	default:
		return fmt.Errorf("%q is not a lifecycle status; the statuses are %s, %s, and %s", req.Value, lifecycle.StatusActive, lifecycle.StatusOnHold, lifecycle.StatusRetired)
	}
	if !m.knownSubject(req.Subject) {
		return fmt.Errorf("%q is neither a releasable or member the declarations declare nor a subject the record holds a lifecycle period of; the declared subjects are %s", req.Subject, strings.Join(m.ws.Declarations.SubjectNames(), ", "))
	}
	on := inv.Now()
	rec := m.record
	if current, ok := rec.LifecycleOn(req.Subject, on); ok && current.Status == status {
		return fmt.Errorf("%q is already %s (since %s); nothing was written", req.Subject, status, day(current.From))
	}
	if err := closeOpen(rec, lifecycle.TableLifecycle, req.Subject, on); err != nil {
		return err
	}
	if err := rec.OpenPeriod(lifecycle.TableLifecycle, req.Subject, string(status), on, req.Reason); err != nil {
		return err
	}
	inv.Say(fmt.Sprintf("%s is %s from %s.", req.Subject, status, day(on)))
	return inv.save(m, rec, on, fmt.Sprintf("transition: %s is %s", req.Subject, status))
}

// knownSubject reports whether subject is declared or held by the record's
// lifecycle table.
func (m managed) knownSubject(subject string) bool {
	for _, name := range m.ws.Declarations.SubjectNames() {
		if name == subject {
			return true
		}
	}
	for _, l := range m.record.Lifecycle() {
		if l.Subject == subject {
			return true
		}
	}
	return false
}

// closeOpen closes the subject's open period of table on the date of on,
// when it has one.
func closeOpen(rec *lifecycle.Record, table lifecycle.Table, subject string, on time.Time) error {
	var open bool
	switch table {
	case lifecycle.TableLifecycle:
		for _, l := range rec.Lifecycle() {
			open = open || (l.Subject == subject && l.Open())
		}
	case lifecycle.TableLicenses:
		for _, l := range rec.Licenses() {
			open = open || (l.Subject == subject && l.Open())
		}
	}
	if !open {
		return nil
	}
	return rec.ClosePeriod(table, subject, on)
}

// proprietaryOthers are the releasables other than subject whose license
// on the date of on is proprietary.
func proprietaryOthers(rec *lifecycle.Record, subject string, on time.Time) []string {
	var out []string
	for _, l := range rec.Licenses() {
		if l.Subject != subject && l.Proprietary() && l.Contains(on) {
			out = append(out, l.Subject)
		}
	}
	return out
}

// License closes the releasable's license period on the command's date and
// opens one under the new license. proprietary is refused (classify makes a
// releasable proprietary, against GitHub's visibility), and so is a change
// that would leave no releasable proprietary (declassify takes a repository
// public, squashing its proprietary history first).
func (inv Invocation) License(req PeriodRequest) error {
	m, err := inv.openManaged(req.Dir)
	if err != nil {
		return err
	}
	if err := m.requireReleasable(req.Subject); err != nil {
		return err
	}
	license := strings.TrimSpace(req.Value)
	if license == "" || license != req.Value || strings.ContainsAny(license, " \t\n") {
		return fmt.Errorf("--license %q is not a license identifier; name an SPDX identifier (MIT, Apache-2.0, ...)", req.Value)
	}
	if license == lifecycle.ProprietaryLicense {
		return fmt.Errorf("a releasable is made proprietary by `rlsbl transition classify --subject %s --reason <why>`, which first checks that GitHub reports the repository private; nothing was written", req.Subject)
	}
	on := inv.Now()
	rec := m.record
	current, ok := rec.LicenseOn(req.Subject, on)
	if ok && current.License == license {
		return fmt.Errorf("%q is already licensed %s (since %s); nothing was written", req.Subject, license, day(current.From))
	}
	if ok && current.Proprietary() && len(proprietaryOthers(rec, req.Subject, on)) == 0 {
		return fmt.Errorf("%q is the repository's only proprietary releasable, so licensing it %s would make the repository public without squashing its proprietary history; nothing was written. Declassify the repository instead: `rlsbl transition declassify --license %s=%s --reason <why>`", req.Subject, license, req.Subject, license)
	}
	if err := closeOpen(rec, lifecycle.TableLicenses, req.Subject, on); err != nil {
		return err
	}
	if err := rec.OpenPeriod(lifecycle.TableLicenses, req.Subject, license, on, req.Reason); err != nil {
		return err
	}
	inv.Say(fmt.Sprintf("%s is licensed %s from %s.", req.Subject, license, day(on)))
	return inv.save(m, rec, on, fmt.Sprintf("transition: %s is licensed %s", req.Subject, license))
}

// IdentityRequest is a `transition identity`.
type IdentityRequest struct {
	Dir         string
	Subject     string
	Facet       lifecycle.Facet
	Value       string
	Registry    string
	TagPatterns []string
	// From is when the identity started; zero declares it current from the
	// command's date, closing the subject's open identity of the facet.
	From time.Time
	// Until is the first day after a closed identity; zero leaves it open.
	Until  time.Time
	Reason string
}

// Identity declares an identity period. Without a start date the identity
// starts on the command's date and the subject's open identity of the same
// facet closes there; with one it is recorded as given, a dead identity's
// included.
func (inv Invocation) Identity(req IdentityRequest) error {
	m, err := inv.openManaged(req.Dir)
	if err != nil {
		return err
	}
	if !req.Until.IsZero() && req.From.IsZero() {
		return fmt.Errorf("--until closes an identity recorded with its start; pass --from with it")
	}
	on := inv.Now()
	rec := m.record
	id := lifecycle.Identity{
		Subject:     req.Subject,
		Facet:       req.Facet,
		Value:       req.Value,
		Registry:    req.Registry,
		TagPatterns: req.TagPatterns,
		Period:      lifecycle.Period{From: req.From, Until: req.Until},
		Reason:      req.Reason,
	}
	if req.From.IsZero() {
		id.From = on
		if open := openIdentity(rec, req.Subject, req.Facet); open != nil {
			if open.Value == req.Value {
				return fmt.Errorf("%q's %s identity is already %q (since %s); nothing was written", req.Subject, req.Facet, req.Value, day(open.From))
			}
			if err := rec.CloseIdentity(req.Subject, req.Facet, on); err != nil {
				return err
			}
		}
	}
	if err := rec.AddIdentity(id); err != nil {
		return err
	}
	inv.Say(fmt.Sprintf("Recorded %s's %s identity %q from %s.", req.Subject, req.Facet, req.Value, day(id.From)))
	return inv.save(m, rec, on, fmt.Sprintf("transition: %s %s identity %s", req.Subject, req.Facet, req.Value))
}

// openIdentity is the subject's open dated identity of facet, or nil.
func openIdentity(rec *lifecycle.Record, subject string, facet lifecycle.Facet) *lifecycle.Identity {
	for _, id := range rec.Identities() {
		if id.Subject == subject && id.Facet == facet && !id.Pending() && id.Open() {
			return &id
		}
	}
	return nil
}

// UnversionedTag records a tag that releases no version, so the readers of
// the tag namespace (the backfill, reconcile, and the tag checks) account
// for it instead of reporting it.
func (inv Invocation) UnversionedTag(dir, tag, reason string) error {
	m, err := inv.openManaged(dir)
	if err != nil {
		return err
	}
	on := inv.Now()
	if err := m.record.AddUnversionedTag(tag, reason, on); err != nil {
		return err
	}
	inv.Say(fmt.Sprintf("Recorded %s as a tag that releases no version.", tag))
	return inv.save(m, m.record, on, "transition: "+tag+" releases no version")
}

// gitHubRepository is the GitHub repository the declarations or the origin
// remote name, and a client to ask it through.
func (inv Invocation) gitHubRepository(m managed) (github.Client, github.Repository, error) {
	url := ""
	configured, err := m.repo.RemoteConfigured(origin)
	if err != nil {
		return github.Client{}, github.Repository{}, err
	}
	if configured {
		if url, err = m.repo.RemoteURL(origin); err != nil {
			return github.Client{}, github.Repository{}, err
		}
	}
	slug, err := github.ResolveRepository(m.ws.Declarations.GitHubRepository, url)
	if err != nil {
		return github.Client{}, github.Repository{}, err
	}
	client, err := github.New(inv.E)
	if err != nil {
		return github.Client{}, github.Repository{}, err
	}
	return client, slug, nil
}

// Classify makes a releasable proprietary: GitHub must already report the
// repository private (making it private is the owner's action, outside
// rlsbl), and then a proprietary license period opens on the command's
// date, which makes the repository confidential and puts its names into the
// confidential-name index.
func (inv Invocation) Classify(dir, subject, reason string) error {
	m, err := inv.openManaged(dir)
	if err != nil {
		return err
	}
	if err := m.requireReleasable(subject); err != nil {
		return err
	}
	on := inv.Now()
	rec := m.record
	if current, ok := rec.LicenseOn(subject, on); ok && current.Proprietary() {
		return fmt.Errorf("%q is already proprietary (since %s); nothing was written", subject, day(current.From))
	}
	client, slug, err := inv.gitHubRepository(m)
	if err != nil {
		return err
	}
	visibility, askErr := publishrules.GitHubVisibility(client, slug).Visibility()
	switch visibility {
	case lifecycle.VisibilityPrivate:
	case lifecycle.VisibilityPublic:
		return fmt.Errorf("GitHub reports %s public, and a proprietary releasable requires a private repository; nothing was written. Making the repository private is the owner's decision, outside rlsbl (`gh repo edit %s --visibility private --accept-visibility-change-consequences`, or the repository's settings); then run this again", slug, slug)
	default:
		return fmt.Errorf("the visibility of %s on GitHub could not be had (%v), and a releasable is classified only in a repository GitHub reports private; nothing was written", slug, askErr)
	}
	if err := closeOpen(rec, lifecycle.TableLicenses, subject, on); err != nil {
		return err
	}
	if err := rec.OpenPeriod(lifecycle.TableLicenses, subject, lifecycle.ProprietaryLicense, on, reason); err != nil {
		return err
	}
	inv.Say(fmt.Sprintf("%s is proprietary from %s; the repository is confidential.", subject, day(on)))
	return inv.save(m, rec, on, "transition: classify "+subject)
}

// headRecord is the record HEAD commits, nil when HEAD holds none.
func headRecord(repo git.Repo) (*lifecycle.Record, error) {
	text, found, err := repo.FileAt("HEAD", lifecycle.RecordFile)
	if err != nil || !found {
		return nil, err
	}
	return lifecycle.Parse([]byte(text))
}
