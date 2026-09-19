package fetcher

import (
	"context"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFetch(t *testing.T) {
	t.Run("successful 200 returns body and final URL", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("<html><body>hello</body></html>"))
		}))
		defer srv.Close()

		f := New(5)
		result, err := f.Fetch(context.Background(), srv.URL)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if string(result.Body) != "<html><body>hello</body></html>" {
			t.Errorf("body: got %q", string(result.Body))
		}
		// FinalURL should be the server URL (no redirects here).
		if !strings.HasPrefix(result.FinalURL, "http://127.0.0.1") {
			t.Errorf("finalURL: got %q, expected localhost address", result.FinalURL)
		}
	})

	t.Run("404 response returns error", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusNotFound)
		}))
		defer srv.Close()

		f := New(5)
		_, err := f.Fetch(context.Background(), srv.URL)
		if err == nil {
			t.Fatal("expected error for 404 response, got nil")
		}
		if !strings.Contains(err.Error(), "404") {
			t.Errorf("error should mention 404, got: %v", err)
		}
	})

	t.Run("500 response returns error", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
		}))
		defer srv.Close()

		f := New(5)
		_, err := f.Fetch(context.Background(), srv.URL)
		if err == nil {
			t.Fatal("expected error for 500 response, got nil")
		}
		if !strings.Contains(err.Error(), "500") {
			t.Errorf("error should mention 500, got: %v", err)
		}
	})

	t.Run("redirect: FinalURL is the destination", func(t *testing.T) {
		// srv2 is the redirect target.
		srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("redirected content"))
		}))
		defer srv2.Close()

		// srv1 redirects to srv2.
		srv1 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, srv2.URL+"/final", http.StatusMovedPermanently)
		}))
		defer srv1.Close()

		f := New(5)
		result, err := f.Fetch(context.Background(), srv1.URL)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if !strings.HasSuffix(result.FinalURL, "/final") {
			t.Errorf("FinalURL should end with /final (redirect destination), got %q", result.FinalURL)
		}
		if string(result.Body) != "redirected content" {
			t.Errorf("body: got %q, want %q", string(result.Body), "redirected content")
		}
	})

	t.Run("cancelled context returns error", func(t *testing.T) {
		// srv blocks forever — the test should cancel before it completes.
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// Block until the client disconnects.
			<-r.Context().Done()
		}))
		defer srv.Close()

		ctx, cancel := context.WithCancel(context.Background())
		cancel() // cancel immediately

		f := New(5)
		_, err := f.Fetch(ctx, srv.URL)
		if err == nil {
			t.Fatal("expected error from cancelled context, got nil")
		}
	})

	t.Run("response body is capped at maxBodyBytes", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			// Write more than the limit
			_, _ = w.Write(make([]byte, maxBodySize+1024))
		}))
		defer srv.Close()

		f := New(5)
		result, err := f.Fetch(context.Background(), srv.URL)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if int64(len(result.Body)) > maxBodySize {
			t.Errorf("body not capped: got %d bytes, want <= %d", len(result.Body), maxBodySize)
		}
	})
}

func TestWithProxy(t *testing.T) {
	t.Run("percent-encoded credentials are decoded before Proxy-Authorization", func(t *testing.T) {
		// Pins the behaviour the playwright worker was aligned to (BUG-017): a
		// reserved character in the password is spelled percent-encoded in
		// proxy_url and must reach the proxy decoded. net/url decodes userinfo
		// and http.ProxyURL builds the header from the decoded form.
		wantUser, wantPass := "us@er", "p@ss:w0rd"
		var gotUser, gotPass string
		var gotOK bool

		proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			gotUser, gotPass, gotOK = parseProxyBasicAuth(r.Header.Get("Proxy-Authorization"))
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("via proxy"))
		}))
		defer proxy.Close()

		f, err := New(5).WithProxy("http://us%40er:p%40ss%3Aw0rd@" + strings.TrimPrefix(proxy.URL, "http://"))
		if err != nil {
			t.Fatalf("WithProxy: %v", err)
		}
		// Plain http so the transport forwards through the proxy rather than CONNECT.
		result, err := f.Fetch(context.Background(), "http://example.invalid/page")
		if err != nil {
			t.Fatalf("Fetch via proxy: %v", err)
		}
		if string(result.Body) != "via proxy" {
			t.Errorf("body: got %q, want %q", string(result.Body), "via proxy")
		}
		if !gotOK {
			t.Fatal("proxy received no parseable Proxy-Authorization header")
		}
		if gotUser != wantUser || gotPass != wantPass {
			t.Errorf("proxy credentials: got %q:%q, want %q:%q", gotUser, gotPass, wantUser, wantPass)
		}
	})

	t.Run("invalid proxy URL returns error", func(t *testing.T) {
		if _, err := New(5).WithProxy("http://[::1"); err == nil {
			t.Fatal("expected error for malformed proxy URL, got nil")
		}
	})
}

// parseProxyBasicAuth decodes a "Basic <base64>" Proxy-Authorization value into
// its username and password, mirroring (*http.Request).BasicAuth for the proxy header.
func parseProxyBasicAuth(header string) (user, pass string, ok bool) {
	const prefix = "Basic "
	if !strings.HasPrefix(header, prefix) {
		return "", "", false
	}
	raw, err := base64.StdEncoding.DecodeString(header[len(prefix):])
	if err != nil {
		return "", "", false
	}
	user, pass, ok = strings.Cut(string(raw), ":")
	return user, pass, ok
}
