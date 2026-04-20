// Package robots fetches and parses robots.txt files to enforce crawl restrictions.
// The HTTP request is always direct — never via the job proxy (spec §3.4).
package robots

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	fetchTimeout = 5 * time.Second
	maxBodySize  = 512 * 1024 // 512 KB — robots.txt files are small in practice
)

// directClient is used for all robots.txt fetches. It is never proxy-configured.
var directClient = &http.Client{}

// IsDisallowed fetches {origin}/robots.txt and returns true if rawURL is disallowed
// for this crawler. Returns (false, nil) on any fetch error — a missing or unreachable
// robots.txt is treated as no restrictions (spec §3.4).
func IsDisallowed(ctx context.Context, rawURL string) (bool, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return false, fmt.Errorf("invalid URL: %w", err)
	}
	robotsURL := fmt.Sprintf("%s://%s/robots.txt", u.Scheme, u.Host)

	// 5-second deadline layered on top of the caller's context so that both
	// the timeout and a worker-shutdown cancellation are respected.
	fetchCtx, cancel := context.WithTimeout(ctx, fetchTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(fetchCtx, http.MethodGet, robotsURL, nil)
	if err != nil {
		return false, nil
	}
	req.Header.Set("User-Agent", "ScrapeFlow/1.0")

	resp, err := directClient.Do(req)
	if err != nil {
		return false, nil // fetch failure → proceed
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode != http.StatusOK {
		return false, nil // 404 / 5xx → no restrictions
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBodySize))
	if err != nil {
		return false, nil
	}

	path := u.Path
	if path == "" {
		path = "/"
	}
	return isPathDisallowed(string(body), path), nil
}

// rule is a single Allow or Disallow directive parsed from robots.txt.
type rule struct {
	path  string
	allow bool // true = Allow, false = Disallow
}

// isPathDisallowed parses robots.txt content and checks whether path is disallowed.
//
// Precedence (standard robots.txt semantics):
//  1. User-agent: ScrapeFlow block takes exclusive precedence over User-agent: *
//     (if a ScrapeFlow block exists, the wildcard block is ignored entirely).
//  2. Within applicable rules, the longest matching prefix wins.
//  3. An empty Disallow means "allow everything" and is skipped.
func isPathDisallowed(content, path string) bool {
	specificRules, wildcardRules, foundSpecific := parseRobots(content)

	var rules []rule
	if foundSpecific {
		rules = specificRules
	} else {
		rules = wildcardRules
	}

	return applyRules(rules, path)
}

// parseRobots scans robots.txt content and returns rules for User-agent: ScrapeFlow
// and User-agent: *, plus a flag indicating whether a ScrapeFlow block was found.
func parseRobots(content string) (specificRules, wildcardRules []rule, foundSpecific bool) {
	lines := strings.Split(content, "\n")

	var currentAgents []string
	var currentRules []rule
	var inDirectives bool // true once we've seen at least one Disallow/Allow line

	flush := func() {
		if len(currentAgents) == 0 {
			return
		}
		for _, agent := range currentAgents {
			switch strings.ToLower(strings.TrimSpace(agent)) {
			case "scrapeflow":
				foundSpecific = true
				specificRules = append(specificRules, currentRules...)
			case "*":
				wildcardRules = append(wildcardRules, currentRules...)
			}
		}
		currentAgents = nil
		currentRules = nil
		inDirectives = false
	}

	for _, line := range lines {
		// Strip inline comments and whitespace.
		if idx := strings.Index(line, "#"); idx >= 0 {
			line = line[:idx]
		}
		line = strings.TrimSpace(line)

		if line == "" {
			flush()
			continue
		}

		lower := strings.ToLower(line)
		switch {
		case strings.HasPrefix(lower, "user-agent:"):
			// A new User-agent: line after directives means a new block — flush first.
			if inDirectives {
				flush()
			}
			agent := strings.TrimSpace(line[len("user-agent:"):])
			currentAgents = append(currentAgents, agent)

		case strings.HasPrefix(lower, "disallow:"):
			inDirectives = true
			p := strings.TrimSpace(line[len("disallow:"):])
			currentRules = append(currentRules, rule{path: p, allow: false})

		case strings.HasPrefix(lower, "allow:"):
			inDirectives = true
			p := strings.TrimSpace(line[len("allow:"):])
			currentRules = append(currentRules, rule{path: p, allow: true})
		}
	}
	flush() // handle file that doesn't end with a blank line

	return specificRules, wildcardRules, foundSpecific
}

// applyRules finds the longest matching rule for path and returns whether it disallows.
// An empty Disallow path is skipped (it means "allow all" per the robots.txt spec).
func applyRules(rules []rule, path string) bool {
	bestLen := -1
	bestAllow := true // no match → allow

	for _, r := range rules {
		if r.path == "" {
			continue // empty Disallow = allow all
		}
		if strings.HasPrefix(path, r.path) && len(r.path) > bestLen {
			bestLen = len(r.path)
			bestAllow = r.allow
		}
	}

	return !bestAllow
}
