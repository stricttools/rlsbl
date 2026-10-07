package github

import (
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// Visibility is GitHub's visibility of a repository.
type Visibility string

// The visibilities GitHub reports.
const (
	Public   Visibility = "public"
	Private  Visibility = "private"
	Internal Visibility = "internal"
)

// RepositoryInfo is what GitHub reports about a repository.
type RepositoryInfo struct {
	FullName   string
	Visibility Visibility
	Archived   bool
	// CanPush is whether the authenticated account may push to it.
	CanPush bool
}

// Info reads the repository's metadata. A visibility GitHub does not name as
// public, private, or internal is refused rather than guessed.
func (c Client) Info(repo Repository) (RepositoryInfo, error) {
	out, err := c.APIGet(repo.apiPath(""), false, "")
	if err != nil {
		return RepositoryInfo{}, err
	}
	var doc struct {
		FullName    string `json:"full_name"`
		Visibility  string `json:"visibility"`
		Private     *bool  `json:"private"`
		Archived    bool   `json:"archived"`
		Permissions *struct {
			Push bool `json:"push"`
		} `json:"permissions"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil {
		return RepositoryInfo{}, fmt.Errorf("GitHub's answer about %s is not the JSON a repository document is: %w", repo, err)
	}
	v := Visibility(doc.Visibility)
	switch v {
	case Public, Private, Internal:
	default:
		return RepositoryInfo{}, fmt.Errorf("GitHub reports the visibility of %s as %q, which is none of public, private, and internal", repo, doc.Visibility)
	}
	if doc.Private != nil && *doc.Private != (v != Public) {
		return RepositoryInfo{}, fmt.Errorf("GitHub reports %s as %s with private=%t, which disagree", repo, v, *doc.Private)
	}
	info := RepositoryInfo{FullName: doc.FullName, Visibility: v, Archived: doc.Archived}
	if doc.Permissions != nil {
		info.CanPush = doc.Permissions.Push
	}
	return info, nil
}

// AuthenticatedUser is the login of the account gh is authenticated as.
func (c Client) AuthenticatedUser() (string, error) {
	out, err := c.APIGet("user", false, ".login")
	if err != nil {
		return "", err
	}
	got := lines(out)
	if len(got) != 1 || strings.TrimSpace(got[0]) == "" {
		return "", errors.New("`gh api user` named no single account login")
	}
	return strings.TrimSpace(got[0]), nil
}

// Topics is the repository's topics, in GitHub's order.
func (c Client) Topics(repo Repository) ([]string, error) {
	out, err := c.APIGet(repo.apiPath("topics"), false, "")
	if err != nil {
		return nil, err
	}
	var doc struct {
		Names *[]string `json:"names"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil || doc.Names == nil {
		return nil, fmt.Errorf("GitHub's answer about the topics of %s carries no names list", repo)
	}
	return *doc.Names, nil
}

// SetTopics replaces the repository's topics with topics.
func (c Client) SetTopics(repo Repository, topics []string, extra ...strictcli.EffectOption) error {
	args := []string{"api", "--method", "PUT", repo.apiPath("topics")}
	if len(topics) == 0 {
		return errors.New("setting no topics at all would clear every topic; refusing")
	}
	for _, t := range topics {
		args = append(args, "-f", "names[]="+t)
	}
	return c.write(nil, extra, args...)
}

// Secret is what GitHub says about one Actions secret.
type Secret struct {
	Present bool
	// UpdatedAt is GitHub's timestamp of the secret's last write, as GitHub
	// wrote it; empty when the answer carried none.
	UpdatedAt string
}

// Secret reads whether the repository has an Actions secret called name. A
// 404 from the API is GitHub's way of saying it does not exist; every other
// failure is an error, because a permission or network failure must never
// read as absence or presence.
func (c Client) Secret(repo Repository, name string) (Secret, error) {
	args := []string{"api", "--method", "GET", repo.apiPath("actions/secrets/" + name)}
	res, err := c.read(probeTimeout, args...)
	if err != nil {
		return Secret{}, err
	}
	if res.code == 0 {
		var doc struct {
			UpdatedAt string `json:"updated_at"`
		}
		if err := json.Unmarshal([]byte(res.stdout), &doc); err != nil {
			return Secret{}, fmt.Errorf("GitHub's answer about the %s secret of %s is not JSON: %w", name, repo, err)
		}
		return Secret{Present: true, UpdatedAt: doc.UpdatedAt}, nil
	}
	if strings.Contains(res.stderr, "(HTTP 404)") {
		return Secret{}, nil
	}
	return Secret{}, failed(args, res)
}

// SetSecret writes value into the repository's Actions secret name. The
// value reaches gh on its standard input, never in an argument.
func (c Client) SetSecret(repo Repository, name string, value []byte, extra ...strictcli.EffectOption) error {
	if len(value) == 0 {
		return fmt.Errorf("refusing to set the %s secret of %s to an empty value", name, repo)
	}
	return c.write(value, extra, "secret", "set", name, "--repo", repo.String())
}

// VisibleRepository is one repository the authenticated account can see.
type VisibleRepository struct {
	Repository Repository
	Archived   bool
}

// VisibleRepositories is every repository the authenticated account owns or
// can see through an organization it belongs to, each once, sorted by slug.
func (c Client) VisibleRepositories() ([]VisibleRepository, error) {
	if _, err := c.AuthenticatedUser(); err != nil {
		return nil, err
	}
	paths := []string{"user/repos?affiliation=owner&per_page=100"}
	orgs, err := c.APIGet("user/orgs?per_page=100", true, ".[].login")
	if err != nil {
		return nil, err
	}
	for _, org := range lines(orgs) {
		paths = append(paths, "orgs/"+strings.TrimSpace(org)+"/repos?per_page=100")
	}
	seen := map[string]VisibleRepository{}
	for _, p := range paths {
		out, err := c.APIGet(p, true, `.[] | "\(.full_name)\t\(.archived)"`)
		if err != nil {
			return nil, err
		}
		for _, line := range lines(out) {
			slug, archived, ok := strings.Cut(line, "\t")
			if !ok {
				return nil, fmt.Errorf("the repository listing %s printed a line without a tab: %q", p, line)
			}
			repo, err := ParseRepository(strings.TrimSpace(slug))
			if err != nil {
				return nil, err
			}
			isArchived, err := strconv.ParseBool(strings.TrimSpace(archived))
			if err != nil {
				return nil, fmt.Errorf("the repository listing %s gave %s an archived flag of %q", p, slug, archived)
			}
			seen[repo.String()] = VisibleRepository{Repository: repo, Archived: isArchived}
		}
	}
	keys := make([]string, 0, len(seen))
	for k := range seen {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]VisibleRepository, 0, len(keys))
	for _, k := range keys {
		out = append(out, seen[k])
	}
	return out, nil
}

// FoundRepository is one repository a search returned.
type FoundRepository struct {
	FullName    string `json:"full_name"`
	Description string `json:"description"`
	UpdatedAt   string `json:"updated_at"`
	Owner       string `json:"owner"`
}

// searchTotal is the tag of the line carrying a search's total count.
const searchTotal = "total"

// SearchRepositoriesByTopic is every repository carrying the topic, most
// recently updated first. GitHub's search returns at most 1000 results; a
// topic carried by more repositories than the listing holds is refused, so
// the listing is never silently incomplete.
func (c Client) SearchRepositoriesByTopic(topic string) ([]FoundRepository, error) {
	if !slugPart.MatchString(topic) {
		return nil, fmt.Errorf("%q is not a GitHub topic", topic)
	}
	jq := `"` + searchTotal + `\t\(.total_count)", (.items[] | [.full_name, (.description // ""), .updated_at, .owner.login] | @tsv)`
	out, err := c.APIGet("search/repositories?q=topic:"+topic+"&sort=updated&per_page=100", true, jq)
	if err != nil {
		return nil, err
	}
	total := -1
	var found []FoundRepository
	for _, line := range lines(out) {
		fields := strings.Split(line, "\t")
		if fields[0] == searchTotal && len(fields) == 2 {
			n, err := strconv.Atoi(fields[1])
			if err != nil {
				return nil, fmt.Errorf("the search printed a total that is not a number: %q", line)
			}
			total = max(total, n)
			continue
		}
		if len(fields) != 4 {
			return nil, fmt.Errorf("the search printed a line that is not four tab-separated fields: %q", line)
		}
		for i := range fields {
			fields[i] = unescapeTSV(fields[i])
		}
		found = append(found, FoundRepository{FullName: fields[0], Description: fields[1], UpdatedAt: fields[2], Owner: fields[3]})
	}
	if total < 0 {
		return nil, errors.New("the search printed no total count")
	}
	if total > len(found) {
		return nil, fmt.Errorf("GitHub reports %d repositories carrying the %s topic but returned %d (its search returns at most 1000), so the listing would be incomplete", total, topic, len(found))
	}
	return found, nil
}

// unescapeTSV undoes jq's @tsv escapes.
func unescapeTSV(s string) string {
	if !strings.Contains(s, `\`) {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] != '\\' || i+1 == len(s) {
			b.WriteByte(s[i])
			continue
		}
		i++
		switch s[i] {
		case 't':
			b.WriteByte('\t')
		case 'n':
			b.WriteByte('\n')
		case 'r':
			b.WriteByte('\r')
		case '\\':
			b.WriteByte('\\')
		default:
			b.WriteByte('\\')
			b.WriteByte(s[i])
		}
	}
	return b.String()
}

// EnsureTopic adds topic to the repository's topics, keeping every other
// one; changed is false when the repository already carries it.
func (c Client) EnsureTopic(repo Repository, topic string, extra ...strictcli.EffectOption) (changed bool, err error) {
	topics, err := c.Topics(repo)
	if err != nil {
		return false, err
	}
	if slices.Contains(topics, topic) {
		return false, nil
	}
	if err := c.SetTopics(repo, append(topics, topic), extra...); err != nil {
		return false, err
	}
	return true, nil
}
