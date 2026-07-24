// Unit tests for handleMessage behaviour around robots.txt and proxy routing.
// These tests use a plain nats.Msg (no JetStream metadata) — Ack() errors are
// logged and swallowed, which is fine because we are testing publish behaviour
// only.
package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/fernet/fernet-go"
	"github.com/nats-io/nats.go"

	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
)

// mockStorage implements storageClient for unit tests.
// It always returns a dummy MinIO path and never errors.
type mockStorage struct{}

func (m *mockStorage) Upload(_ context.Context, jobID, ext string, _ []byte) (string, error) {
	return fmt.Sprintf("test-bucket/history/%s/1234567890.%s", jobID, ext), nil
}

// robotsServer returns an httptest.Server that serves a robots.txt disallowing
// disallowedPath for all user-agents.  Other paths return 200.
func robotsServer(t *testing.T, disallowedPath string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/robots.txt" {
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, "User-agent: *\nDisallow: %s\n", disallowedPath)
			return
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "<html><body>content</body></html>")
	}))
}

// makeMsg serialises job into a nats.Msg — the minimal shape handleMessage needs.
func makeMsg(t *testing.T, job ScrapeMessage) *nats.Msg {
	t.Helper()
	data, err := json.Marshal(job)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	return &nats.Msg{Data: data}
}

// decodeResult deserialises a published payload captured by mockJS.
func decodeResult(t *testing.T, data []byte) resultMessage {
	t.Helper()
	var r resultMessage
	if err := json.Unmarshal(data, &r); err != nil {
		t.Fatalf("decode resultMessage: %v", err)
	}
	return r
}

func TestHandleMessage_RobotsDisallowed(t *testing.T) {
	srv := robotsServer(t, "/secret/")
	defer srv.Close()

	js := &mockJS{}
	w := &Worker{js: js, fetcher: fetcher.New(5), storage: &mockStorage{}}

	msg := makeMsg(t, ScrapeMessage{
		SchemaVersion: 2,
		JobID:         "job-robots-1",
		RunID:         "run-robots-1",
		URL:           srv.URL + "/secret/data",
		OutputFormat:  "html",
		Options:       &Options{RespectRobots: true},
	})

	w.handleMessage(context.Background(), msg, 3)

	// Exactly one publish: the "failed" result.  No "running" event because
	// the robots check fires before the running publish (ADR-004 ordering).
	if js.publishCalls != 1 {
		t.Fatalf("expected 1 publish (failed), got %d", js.publishCalls)
	}
	result := decodeResult(t, js.published[0])
	if result.Status != "failed" {
		t.Errorf("status: got %q, want %q", result.Status, "failed")
	}
	if result.Error != "robots_txt_disallowed" {
		t.Errorf("error: got %q, want %q", result.Error, "robots_txt_disallowed")
	}
	if result.JobID != "job-robots-1" {
		t.Errorf("job_id: got %q, want %q", result.JobID, "job-robots-1")
	}
}

func TestHandleMessage_RobotsAllowed_Proceeds(t *testing.T) {
	// Path /public/ is not in the disallow list — robots check passes.
	// Execution reaches processJob successfully (mockStorage returns a dummy path).
	// We assert that the robots gate does NOT block this request.
	srv := robotsServer(t, "/secret/")
	defer srv.Close()

	js := &mockJS{}
	w := &Worker{js: js, fetcher: fetcher.New(5), storage: &mockStorage{}}

	msg := makeMsg(t, ScrapeMessage{
		SchemaVersion: 2,
		JobID:         "job-robots-2",
		RunID:         "run-robots-2",
		URL:           srv.URL + "/public/page",
		OutputFormat:  "html",
		Options:       &Options{RespectRobots: true},
	})

	w.handleMessage(context.Background(), msg, 3)

	// Regardless of how processJob fails, the first publish must be "running"
	// (not "robots_txt_disallowed"), proving the robots gate let it through.
	if js.publishCalls == 0 {
		t.Fatal("expected at least one publish, got 0")
	}
	first := decodeResult(t, js.published[0])
	if first.Status == "failed" && first.Error == "robots_txt_disallowed" {
		t.Errorf("allowed path was incorrectly blocked by robots.txt check")
	}
	if first.Status != "running" {
		t.Errorf("first publish status: got %q, want %q", first.Status, "running")
	}
}

func TestHandleMessage_RespectRobotsFalse_SkipsCheck(t *testing.T) {
	// robots.txt disallows everything — but respect_robots is false so it is ignored.
	srv := robotsServer(t, "/")
	defer srv.Close()

	js := &mockJS{}
	w := &Worker{js: js, fetcher: fetcher.New(5), storage: &mockStorage{}}

	msg := makeMsg(t, ScrapeMessage{
		SchemaVersion: 2,
		JobID:         "job-robots-3",
		RunID:         "run-robots-3",
		URL:           srv.URL + "/any/path",
		OutputFormat:  "html",
		// Options nil (respect_robots defaults to false)
	})

	w.handleMessage(context.Background(), msg, 3)

	if js.publishCalls == 0 {
		t.Fatal("expected at least one publish, got 0")
	}
	first := decodeResult(t, js.published[0])
	if first.Error == "robots_txt_disallowed" {
		t.Error("robots.txt check ran despite respect_robots=false")
	}
	if first.Status != "running" {
		t.Errorf("first publish status: got %q, want %q", first.Status, "running")
	}
}

func TestHandleMessage_MalformedProxyURL_FailsWithoutRetry(t *testing.T) {
	// A malformed proxy URL is a config error — fail run immediately, no retry.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	// Generate a test key and encrypt the malformed proxy URL — worker decrypts before parsing.
	var testKey fernet.Key
	if err := testKey.Generate(); err != nil {
		t.Fatalf("failed to generate test fernet key: %v", err)
	}
	token, err := fernet.EncryptAndSign([]byte("://not-a-url"), &testKey)
	if err != nil {
		t.Fatalf("failed to encrypt test proxy URL: %v", err)
	}

	js := &mockJS{}
	w := &Worker{js: js, fetcher: fetcher.New(5), storage: &mockStorage{}, credentialsKey: &testKey}

	msg := makeMsg(t, ScrapeMessage{
		SchemaVersion: 2,
		JobID:         "job-proxy-bad",
		RunID:         "run-proxy-bad",
		URL:           srv.URL,
		OutputFormat:  "html",
		Credentials:   &Credentials{EncryptedProxyURL: string(token)},
	})

	w.handleMessage(context.Background(), msg, 3)

	if js.publishCalls != 1 {
		t.Fatalf("expected 1 publish (failed), got %d", js.publishCalls)
	}
	result := decodeResult(t, js.published[0])
	if result.Status != "failed" {
		t.Errorf("status: got %q, want %q", result.Status, "failed")
	}
	if result.Error == "" {
		t.Error("error field should not be empty for malformed proxy URL")
	}
}
