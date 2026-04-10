package fetcher

import (
	"context"
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
