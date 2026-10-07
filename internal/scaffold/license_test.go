package scaffold

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// licensed is a member context whose releasable, portal, has license on the
// scaffold's date ("" for none), named by author.
func licensed(license, author string) *memberContext {
	return &memberContext{versioned: true, releasable: declarations.Releasable{Name: "portal"}, license: license, author: author, now: today}
}

func TestTheLicenseDecision(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		name   string
		c      *memberContext
		reason string
	}{
		{"versioned under none", &memberContext{now: today}, "versioned under no releasable"},
		{"no license in the record", licensed("", "Ada"), "holds no license in effect"},
		{"proprietary", licensed("proprietary", "Ada"), "is proprietary"},
		{"a license rlsbl carries no text of", licensed("Apache-2.0", "Ada"), "carries no text of Apache-2.0"},
	}
	for _, c := range cases {
		text, reason, err := c.c.licenseDecision()
		if err != nil || text != "" || !strings.Contains(reason, c.reason) {
			t.Errorf("%s: %q, %q, %v", c.name, text, reason, err)
		}
	}
	text, reason, err := licensed("MIT", "Ada Lovelace").licenseDecision()
	if err != nil || reason != "" || !strings.HasPrefix(text, "MIT License\n\nCopyright (c) 2026 Ada Lovelace\n") {
		t.Fatalf("MIT: %q, %q, %v", text, reason, err)
	}
	if _, _, err := licensed("MIT", "").licenseDecision(); err == nil || !strings.Contains(err.Error(), "git config user.name") {
		t.Fatalf("MIT without a copyright holder: %v", err)
	}
}

// Every license rlsbl carries the text of renders with nothing left over.
func TestEveryCarriedLicenseRenders(t *testing.T) {
	hygiene.Isolate(t)
	for license := range licenseTemplates {
		text, _, err := licensed(license, "Ada Lovelace").licenseDecision()
		if err != nil || !strings.Contains(text, "Ada Lovelace") || strings.Contains(text, "{{") {
			t.Errorf("%s: %v\n%s", license, err, text)
		}
	}
}
