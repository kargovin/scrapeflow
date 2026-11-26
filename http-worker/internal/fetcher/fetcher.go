// Package fetcher performs HTTP GET requests and returns the raw response body.
package fetcher

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"time"
)

const maxBodySize = 10 * 1024 * 1024 // 10 MB

// refusalHeader is how the egress proxy (Smokescreen) names a policy refusal on its 407.
const refusalHeader = "X-Smokescreen-Error"

// Result holds the raw HTML bytes and the final URL after any redirects.
type Result struct {
	Body     []byte // raw HTML response body
	FinalURL string // URL after following redirects (may differ from the requested URL)
}

// RefusedError is the egress proxy refusing an address: the target (or a redirect, or the job's
// own proxy) resolves to an address that is not public unicast. Final — a retry resolves the same
// name again.
type RefusedError struct {
	Reason string
}

func (e *RefusedError) Error() string { return "address refused: " + e.Reason }

// Fetcher performs HTTP GET requests with a configurable timeout.
// It is safe to use from multiple goroutines.
type Fetcher struct {
	client *http.Client
	egress *url.URL
}

// New creates a Fetcher with the given per-request timeout whose every connection goes through
// the egress proxy (BUG-010 part 2): the proxy resolves each name, refuses a non-public address and
// connects to the address it checked — redirects included, since each hop is a new request.
// A nil egress connects directly; only tests do that.
// Always use this rather than http.Get() — the default http client has no timeout.
func New(timeoutSecs int, egress *url.URL) *Fetcher {
	transport := &http.Transport{}
	if egress != nil {
		transport.Proxy = http.ProxyURL(egress)
		transport.OnProxyConnectResponse = refuseOnConnect
	}
	return &Fetcher{
		client: &http.Client{
			Transport: transport,
			Timeout:   time.Duration(timeoutSecs) * time.Second,
		},
		egress: egress,
	}
}

// Client is the fetcher's egress-only client, for fetches that must never use a job's proxy
// (robots.txt).
func (f *Fetcher) Client() *http.Client { return f.client }

// WithProxy returns a new Fetcher that routes all requests through the given proxy URL.
// The job's proxy is reached through a tunnel the egress proxy opens, so its address is checked
// like any other; what the job's proxy then fetches happens on its side.
// The original Fetcher's timeout is preserved on the new client.
func (f *Fetcher) WithProxy(rawProxyURL string) (*Fetcher, error) {
	proxyURL, err := url.Parse(rawProxyURL)
	if err != nil {
		return nil, fmt.Errorf("invalid proxy URL: %w", err)
	}
	transport := &http.Transport{Proxy: http.ProxyURL(proxyURL)}
	if f.egress != nil {
		transport.DialContext = tunnelThrough(f.egress)
	}
	return &Fetcher{
		client: &http.Client{Transport: transport, Timeout: f.client.Timeout},
		egress: f.egress,
	}, nil
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

	if f.egress != nil {
		if err := refusal(resp); err != nil {
			return nil, fmt.Errorf("fetching %s: %w", rawURL, err)
		}
	}

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

// refusal is the egress proxy's refusal carried by resp, or nil. A 407 without the header is a
// job proxy asking for credentials, not a refusal.
func refusal(resp *http.Response) error {
	if resp.StatusCode != http.StatusProxyAuthRequired {
		return nil
	}
	if reason := resp.Header.Get(refusalHeader); reason != "" {
		return &RefusedError{Reason: reason}
	}
	return nil
}

// refuseOnConnect turns the egress proxy's refusal of an https CONNECT into a RefusedError;
// net/http otherwise reports only the status text.
func refuseOnConnect(_ context.Context, _ *url.URL, _ *http.Request, resp *http.Response) error {
	return refusal(resp)
}

// tunnelThrough dials an address by asking the egress proxy for a CONNECT tunnel to it.
func tunnelThrough(egress *url.URL) func(ctx context.Context, network, addr string) (net.Conn, error) {
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		var d net.Dialer
		conn, err := d.DialContext(ctx, network, egress.Host)
		if err != nil {
			return nil, fmt.Errorf("dialing egress proxy: %w", err)
		}
		if deadline, ok := ctx.Deadline(); ok {
			_ = conn.SetDeadline(deadline)
		}
		req := &http.Request{
			Method: http.MethodConnect,
			URL:    &url.URL{Opaque: addr},
			Host:   addr,
			Header: http.Header{},
		}
		if err := req.Write(conn); err != nil {
			_ = conn.Close()
			return nil, fmt.Errorf("writing CONNECT: %w", err)
		}
		br := bufio.NewReader(conn)
		resp, err := http.ReadResponse(br, req)
		if err != nil {
			_ = conn.Close()
			return nil, fmt.Errorf("reading CONNECT response: %w", err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			_ = conn.Close()
			if err := refusal(resp); err != nil {
				return nil, err
			}
			return nil, fmt.Errorf("egress proxy CONNECT to %s: %s", addr, resp.Status)
		}
		_ = conn.SetDeadline(time.Time{})
		if br.Buffered() > 0 {
			return &bufferedConn{Conn: conn, r: br}, nil
		}
		return conn, nil
	}
}

// bufferedConn serves bytes the CONNECT response read past its end before the connection's own.
type bufferedConn struct {
	net.Conn
	r *bufio.Reader
}

func (c *bufferedConn) Read(p []byte) (int, error) { return c.r.Read(p) }

// IsRefused reports whether err carries an egress refusal.
func IsRefused(err error) bool {
	var re *RefusedError
	return errors.As(err, &re)
}
