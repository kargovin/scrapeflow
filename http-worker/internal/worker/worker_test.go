//go:build integration

// Tests in package worker (same package) can access unexported methods like processJob.
package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/kargovin/scrapeflow/worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/worker/internal/storage"
)

// minioEndpoint returns the MinIO address (Docker Compose default: localhost:9000).
func minioEndpoint() string {
	if v := os.Getenv("MINIO_ENDPOINT"); v != "" {
		return v
	}
	return "localhost:9000"
}

// newTestWorker wires a real storage.Client and fetcher.Fetcher into a Worker.
// No NATS connection is needed — we call processJob directly, which does not touch js.
func newTestWorker(t *testing.T, bucket string) *Worker {
	t.Helper()
	store, err := storage.New(minioEndpoint(), "scrapeflow", "scrapeflow_secret", bucket, false)
	if err != nil {
		t.Fatalf("storage.New: %v", err)
	}
	f := fetcher.New(10)
	return &Worker{js: nil, fetcher: f, storage: store}
}

// newVerifyClient creates a raw MinIO client used only to verify that objects
// were stored correctly. Separate from the storage.Client so we don't need
// to export internal methods from the storage package.
func newVerifyClient(t *testing.T) *minio.Client {
	t.Helper()
	mc, err := minio.New(minioEndpoint(), &minio.Options{
		Creds:  credentials.NewStaticV4("scrapeflow", "scrapeflow_secret", ""),
		Secure: false,
	})
	if err != nil {
		t.Fatalf("minio.New (verify client): %v", err)
	}
	return mc
}

func uniqueJobID() string {
	return fmt.Sprintf("test-job-%d", time.Now().UnixNano())
}

// sampleHTML is served by the httptest.Server in all processJob tests.
const sampleHTML = `<!DOCTYPE html>
<html>
<head><title>Integration Test Page</title></head>
<body><p>Hello from the test server.</p></body>
</html>`

func TestProcessJob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/fail500" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, sampleHTML)
	}))
	defer srv.Close()

	bucket := fmt.Sprintf("test-worker-%d", time.Now().UnixNano())
	mc := newVerifyClient(t)

	tests := []struct {
		name         string
		outputFormat string
		url          string
		wantErr      bool
		checkContent func(t *testing.T, body string)
	}{
		{
			name:         "html format stores raw HTML",
			outputFormat: "html",
			url:          srv.URL,
			checkContent: func(t *testing.T, body string) {
				if !strings.Contains(body, "Hello from the test server") {
					t.Errorf("html result should contain page text, got: %q", body)
				}
			},
		},
		{
			name:         "markdown format stores markdown without HTML tags",
			outputFormat: "markdown",
			url:          srv.URL,
			checkContent: func(t *testing.T, body string) {
				if strings.Contains(body, "<html>") || strings.Contains(body, "<body>") {
					t.Errorf("markdown result should not contain HTML tags, got: %q", body)
				}
				if strings.TrimSpace(body) == "" {
					t.Errorf("markdown result should not be empty")
				}
			},
		},
		{
			name:         "json format stores valid JSON with title and text",
			outputFormat: "json",
			url:          srv.URL,
			checkContent: func(t *testing.T, body string) {
				var out map[string]string
				if err := json.Unmarshal([]byte(body), &out); err != nil {
					t.Fatalf("json result is not valid JSON: %v — body: %s", err, body)
				}
				if out["title"] != "Integration Test Page" {
					t.Errorf("title: got %q, want %q", out["title"], "Integration Test Page")
				}
				if !strings.Contains(out["text"], "Hello from the test server") {
					t.Errorf("text: got %q", out["text"])
				}
				if out["url"] == "" {
					t.Errorf("url field should not be empty")
				}
			},
		},
		{
			name:         "unreachable URL returns error",
			outputFormat: "html",
			url:          "http://127.0.0.1:19999", // nothing listening here
			wantErr:      true,
		},
		{
			name:         "server 500 returns error",
			outputFormat: "html",
			url:          srv.URL + "/fail500",
			wantErr:      true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			w := newTestWorker(t, bucket)
			jobID := uniqueJobID()

			job := &jobMessage{
				JobID:        jobID,
				URL:          tc.url,
				OutputFormat: tc.outputFormat,
			}

			minioPath, err := w.processJob(context.Background(), job)

			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got nil (minioPath=%q)", minioPath)
				}
				return
			}

			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			expectedPrefix := fmt.Sprintf("%s/history/%s/", bucket, jobID)
			if !strings.HasPrefix(minioPath, expectedPrefix) {
				t.Errorf("minioPath: got %q, want prefix %q", minioPath, expectedPrefix)
			}

			// Retrieve the object from MinIO using the verification client.
			// minioPath = "{bucket}/{jobID}.{ext}" — extract just the object name.
			parts := strings.SplitN(minioPath, "/", 2)
			if len(parts) != 2 {
				t.Fatalf("unexpected minioPath format: %q", minioPath)
			}
			objectName := parts[1]

			obj, err := mc.GetObject(context.Background(), bucket, objectName, minio.GetObjectOptions{})
			if err != nil {
				t.Fatalf("GetObject: %v", err)
			}
			defer obj.Close()

			bodyBytes, err := io.ReadAll(obj)
			if err != nil {
				t.Fatalf("reading object: %v", err)
			}

			if tc.checkContent != nil {
				tc.checkContent(t, string(bodyBytes))
			}
		})
	}
}
