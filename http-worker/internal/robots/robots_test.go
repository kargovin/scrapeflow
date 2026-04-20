package robots

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

// --- Parser unit tests (no HTTP) ---

func TestIsPathDisallowed(t *testing.T) {
	tests := []struct {
		name    string
		content string
		path    string
		want    bool
	}{
		{
			name: "disallowed path",
			content: `User-agent: *
Disallow: /private/`,
			path: "/private/data",
			want: true,
		},
		{
			name: "allowed path (different prefix)",
			content: `User-agent: *
Disallow: /private/`,
			path: "/public/data",
			want: false,
		},
		{
			name: "empty disallow means allow all",
			content: `User-agent: *
Disallow:`,
			path: "/anything",
			want: false,
		},
		{
			name: "disallow root blocks everything",
			content: `User-agent: *
Disallow: /`,
			path: "/some/page",
			want: true,
		},
		{
			name: "allow overrides disallow via longer match",
			content: `User-agent: *
Disallow: /admin/
Allow: /admin/login`,
			path: "/admin/login",
			want: false,
		},
		{
			name: "ScrapeFlow block takes precedence over wildcard",
			content: `User-agent: *
Disallow: /blocked/

User-agent: ScrapeFlow
Disallow: /scrapeflow-only/`,
			path: "/blocked/page",
			want: false, // wildcard rule ignored — ScrapeFlow block exists
		},
		{
			name: "ScrapeFlow-specific disallow applies",
			content: `User-agent: *
Disallow: /blocked/

User-agent: ScrapeFlow
Disallow: /scrapeflow-only/`,
			path: "/scrapeflow-only/secret",
			want: true,
		},
		{
			name: "ScrapeFlow block with no directives allows everything",
			content: `User-agent: *
Disallow: /blocked/

User-agent: ScrapeFlow
Disallow:`,
			path: "/blocked/page",
			want: false, // ScrapeFlow block found (empty) → wildcard ignored → allow
		},
		{
			name:    "empty robots.txt allows all",
			content: ``,
			path:    "/anything",
			want:    false,
		},
		{
			name: "comments are stripped",
			content: `# Block crawlers from admin
User-agent: * # applies to all
Disallow: /admin/ # admin area`,
			path: "/admin/users",
			want: true,
		},
		{
			name: "file without trailing newline is parsed correctly",
			content: `User-agent: *
Disallow: /no-newline`,
			path: "/no-newline",
			want: true,
		},
		{
			name: "multiple disallow rules — only matching one applies",
			content: `User-agent: *
Disallow: /a/
Disallow: /b/
Disallow: /c/`,
			path: "/b/page",
			want: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := isPathDisallowed(tc.content, tc.path)
			if got != tc.want {
				t.Errorf("isPathDisallowed(%q) = %v, want %v", tc.path, got, tc.want)
			}
		})
	}
}

// --- Integration tests via httptest.NewServer ---

func TestIsDisallowed_FetchSuccess(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("User-agent: *\nDisallow: /secret/\n"))
	}))
	defer srv.Close()

	t.Run("disallowed path returns true", func(t *testing.T) {
		disallowed, err := IsDisallowed(context.Background(), srv.URL+"/secret/data")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if !disallowed {
			t.Error("expected disallowed=true, got false")
		}
	})

	t.Run("allowed path returns false", func(t *testing.T) {
		disallowed, err := IsDisallowed(context.Background(), srv.URL+"/public/page")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if disallowed {
			t.Error("expected disallowed=false, got true")
		}
	})
}

func TestIsDisallowed_FetchFailure_Proceeds(t *testing.T) {
	// Server returns 404 — treated as no robots.txt (proceed).
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	disallowed, err := IsDisallowed(context.Background(), srv.URL+"/any/path")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if disallowed {
		t.Error("404 robots.txt should allow all paths (proceed), got disallowed=true")
	}
}

func TestIsDisallowed_ServerDown_Proceeds(t *testing.T) {
	// Port with nothing listening — connection refused.
	disallowed, err := IsDisallowed(context.Background(), "http://127.0.0.1:19998/some/path")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if disallowed {
		t.Error("unreachable server should allow all paths (proceed), got disallowed=true")
	}
}

func TestIsDisallowed_CancelledContext_Proceeds(t *testing.T) {
	// Blocked server — context cancel fires first.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel immediately

	disallowed, err := IsDisallowed(ctx, srv.URL+"/some/path")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if disallowed {
		t.Error("cancelled context should treat as fetch failure (proceed), got disallowed=true")
	}
}

func TestIsDisallowed_RespectRobotsFalse_NeverCalled(t *testing.T) {
	// This test documents the worker contract: when options.respect_robots = false,
	// IsDisallowed is never called. We verify it here by confirming the function
	// is callable but not that the worker wires it — that is tested in worker_test.go.
	//
	// A server that would disallow the path — but since the caller controls whether
	// to call IsDisallowed at all, this just checks the function behaves deterministically.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("User-agent: *\nDisallow: /\n"))
	}))
	defer srv.Close()

	// When called, it returns true (everything disallowed).
	disallowed, err := IsDisallowed(context.Background(), srv.URL+"/any")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !disallowed {
		t.Error("expected disallowed=true for Disallow: /")
	}
}
