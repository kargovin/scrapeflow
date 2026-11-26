package fetcher

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
)

// fakeEgress stands in for Smokescreen: it refuses any host named in refused with a 407 carrying
// X-Smokescreen-Error, tunnels CONNECT to anything else and forwards plain-http requests.
type fakeEgress struct {
	*httptest.Server
	mu       sync.Mutex
	connects []string
}

func newFakeEgress(t *testing.T, refused ...string) *fakeEgress {
	t.Helper()
	e := &fakeEgress{}
	isRefused := func(hostport string) bool {
		host := hostport
		if h, _, err := net.SplitHostPort(hostport); err == nil {
			host = h
		}
		for _, r := range refused {
			if host == r {
				return true
			}
		}
		return false
	}
	e.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if isRefused(r.Host) {
			w.Header().Set(refusalHeader, "Egress proxying is denied to host '"+r.Host+"': 10.0.0.9 denied by rule 'Deny: Private Range'.")
			w.WriteHeader(http.StatusProxyAuthRequired)
			return
		}
		if r.Method == http.MethodConnect {
			e.mu.Lock()
			e.connects = append(e.connects, r.Host)
			e.mu.Unlock()
			upstream, err := net.Dial("tcp", r.Host)
			if err != nil {
				w.WriteHeader(http.StatusServiceUnavailable)
				return
			}
			client, _, err := w.(http.Hijacker).Hijack()
			if err != nil {
				_ = upstream.Close()
				return
			}
			_, _ = client.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))
			go func() { _, _ = io.Copy(upstream, client); _ = upstream.Close() }()
			go func() { _, _ = io.Copy(client, upstream); _ = client.Close() }()
			return
		}
		r.RequestURI = ""
		resp, err := http.DefaultTransport.RoundTrip(r)
		if err != nil {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		defer resp.Body.Close() //nolint:errcheck
		for k, v := range resp.Header {
			w.Header()[k] = v
		}
		w.WriteHeader(resp.StatusCode)
		_, _ = io.Copy(w, resp.Body)
	}))
	t.Cleanup(e.Server.Close)
	return e
}

func (e *fakeEgress) url(t *testing.T) *url.URL {
	t.Helper()
	u, err := url.Parse(e.URL)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func wantRefused(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected a refusal, got nil")
	}
	if !IsRefused(err) {
		t.Fatalf("expected a RefusedError, got %T: %v", err, err)
	}
	if !strings.Contains(err.Error(), "Deny: Private Range") {
		t.Errorf("the refusal should carry the proxy's reason: %v", err)
	}
}

func TestEgress_PublicSiteIsFetchedThroughTheProxy(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("hello"))
	}))
	defer site.Close()

	res, err := New(5, newFakeEgress(t).url(t)).Fetch(context.Background(), site.URL)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if string(res.Body) != "hello" {
		t.Errorf("body: got %q", res.Body)
	}
}

func TestEgress_RefusedPlainHTTP(t *testing.T) {
	_, err := New(5, newFakeEgress(t, "internal.test").url(t)).Fetch(context.Background(), "http://internal.test/")
	wantRefused(t, err)
}

func TestEgress_RefusedHTTPS(t *testing.T) {
	// https is a CONNECT: net/http reports only the status text unless OnProxyConnectResponse says more.
	_, err := New(5, newFakeEgress(t, "internal.test").url(t)).Fetch(context.Background(), "https://internal.test/")
	wantRefused(t, err)
}

func TestEgress_RedirectToARefusedAddressIsRefused(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://internal.test/latest/meta-data/", http.StatusFound)
	}))
	defer site.Close()

	_, err := New(5, newFakeEgress(t, "internal.test").url(t)).Fetch(context.Background(), site.URL)
	wantRefused(t, err)
}

func TestEgress_JobProxyIsReachedThroughATunnel(t *testing.T) {
	jobProxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("via job proxy for " + r.Host))
	}))
	defer jobProxy.Close()
	egress := newFakeEgress(t)

	f, err := New(5, egress.url(t)).WithProxy(jobProxy.URL)
	if err != nil {
		t.Fatalf("WithProxy: %v", err)
	}
	res, err := f.Fetch(context.Background(), "http://example.invalid/page")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if string(res.Body) != "via job proxy for example.invalid" {
		t.Errorf("body: got %q", res.Body)
	}
	jobProxyHost := strings.TrimPrefix(jobProxy.URL, "http://")
	if len(egress.connects) != 1 || egress.connects[0] != jobProxyHost {
		t.Errorf("the egress proxy should have tunnelled to the job proxy only: %v", egress.connects)
	}
}

func TestEgress_PrivateJobProxyIsRefused(t *testing.T) {
	f, err := New(5, newFakeEgress(t, "proxy.internal").url(t)).WithProxy("http://u:p@proxy.internal:3128")
	if err != nil {
		t.Fatalf("WithProxy: %v", err)
	}
	_, err = f.Fetch(context.Background(), "https://example.com/")
	wantRefused(t, err)
}

func TestEgress_JobProxyAskingForCredentialsIsNotARefusal(t *testing.T) {
	jobProxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Proxy-Authenticate", `Basic realm="p"`)
		w.WriteHeader(http.StatusProxyAuthRequired)
	}))
	defer jobProxy.Close()

	f, err := New(5, newFakeEgress(t).url(t)).WithProxy(jobProxy.URL)
	if err != nil {
		t.Fatalf("WithProxy: %v", err)
	}
	_, err = f.Fetch(context.Background(), "http://example.invalid/")
	if err == nil || IsRefused(err) {
		t.Fatalf("a job proxy's own 407 is an ordinary failure, got %v", err)
	}
}
