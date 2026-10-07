package tagging

import (
	"fmt"
	"time"

	"github.com/stricttools/rlsbl/internal/github"
)

// Discover lists the repositories carrying the rlsbl topic, most recently
// updated first; with mine, only those the authenticated account owns.
func Discover(gh github.Client, mine bool) ([]github.FoundRepository, error) {
	found, err := gh.SearchRepositoriesByTopic(Keyword)
	if err != nil {
		return nil, fmt.Errorf("could not search GitHub through gh (%w); run `gh auth login`, or set GH_TOKEN, and try again", err)
	}
	if !mine {
		return found, nil
	}
	user, err := gh.AuthenticatedUser()
	if err != nil {
		return nil, fmt.Errorf("could not determine the authenticated GitHub account: %w", err)
	}
	var own []github.FoundRepository
	for _, r := range found {
		if r.Owner == user {
			own = append(own, r)
		}
	}
	return own, nil
}

// RelativeTime renders an RFC 3339 timestamp as its distance before now:
// "just now", "5m ago", "3h ago", "2d ago", "3w ago", "4mo ago", "2y ago".
// An empty timestamp renders empty; one that does not parse is an error.
func RelativeTime(timestamp string, now time.Time) (string, error) {
	if timestamp == "" {
		return "", nil
	}
	t, err := time.Parse(time.RFC3339, timestamp)
	if err != nil {
		return "", fmt.Errorf("the timestamp %q is not RFC 3339", timestamp)
	}
	seconds := int64(now.Sub(t).Seconds())
	minutes := seconds / 60
	hours := minutes / 60
	days := hours / 24
	switch {
	case seconds < 60:
		return "just now", nil
	case minutes < 60:
		return fmt.Sprintf("%dm ago", minutes), nil
	case hours < 24:
		return fmt.Sprintf("%dh ago", hours), nil
	case days < 7:
		return fmt.Sprintf("%dd ago", days), nil
	case days/7 < 5:
		return fmt.Sprintf("%dw ago", days/7), nil
	case days/30 < 12:
		return fmt.Sprintf("%dmo ago", days/30), nil
	}
	return fmt.Sprintf("%dy ago", days/365), nil
}
