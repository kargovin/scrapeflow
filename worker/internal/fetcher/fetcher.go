// Package fetcher performs HTTP GET requests and returns the raw response body.
package fetcher

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"
)

const maxBodySize = 10 * 1024 * 1024 // 10 MB

// Result holds the raw HTML bytes and the final URL after any redirects.
type Result struct {
	Body     []byte // raw HTML response body
	FinalURL string // URL after following redirects (may differ from the requested URL)
}

// Fetcher performs HTTP GET requests with a configurable timeout.
// It is safe to use from multiple goroutines.
type Fetcher struct {
	client *http.Client
}

// New creates a Fetcher with the given per-request timeout.
// Always use this rather than http.Get() — the default http client has no timeout.
func New(timeoutSecs int) *Fetcher {
	return &Fetcher{
		client: &http.Client{
			Timeout: time.Duration(timeoutSecs) * time.Second,
			// Go's http.Client follows redirects automatically (up to 10 by default).
			// We override CheckRedirect only to capture the final URL — the default
			// behaviour (follow redirects) is preserved.
		},
	}
}

// Fetch retrieves the URL and returns the body bytes and final URL.
// ctx allows the caller to cancel the request (e.g. if the worker is shutting down).
func (f *Fetcher) Fetch(ctx context.Context, rawURL string) (*Result, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, fmt.Errorf("building request: %w", err)
	}

	// Set a realistic browser-like User-Agent to avoid trivial bot blocks.
	req.Header.Set("User-Agent", "Mozilla/5.0 (compatible; ScrapeFlow/1.0)")

	resp, err := f.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetching %s: %w", rawURL, err)
	}
	defer resp.Body.Close() //nolint:errcheck // best-effort cleanup

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("non-2xx response from %s: %d", rawURL, resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBodySize))
	if err != nil {
		return nil, fmt.Errorf("reading body: %w", err)
	}

	// resp.Request.URL is the final URL after all redirects.
	finalURL := resp.Request.URL.String()

	return &Result{Body: body, FinalURL: finalURL}, nil
}
