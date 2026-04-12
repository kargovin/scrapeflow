//go:build integration

package storage

import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
)

// minioEndpoint returns the MinIO address (Docker Compose default: localhost:9000).
func minioEndpoint() string {
	if v := os.Getenv("MINIO_ENDPOINT"); v != "" {
		return v
	}
	return "localhost:9000"
}

// newTestClient creates a storage.Client against the local Docker Compose MinIO.
func newTestClient(t *testing.T, bucket string) *Client {
	t.Helper()
	c, err := New(minioEndpoint(), "scrapeflow", "scrapeflow_secret", bucket, false)
	if err != nil {
		t.Fatalf("storage.New: %v", err)
	}
	return c
}

func uniqueJobID() string {
	return fmt.Sprintf("test-job-%d", time.Now().UnixNano())
}

func TestNew(t *testing.T) {
	t.Run("connects and creates bucket", func(t *testing.T) {
		bucket := fmt.Sprintf("test-bucket-%d", time.Now().UnixNano())
		c := newTestClient(t, bucket)
		if c == nil {
			t.Fatal("expected non-nil client")
		}
	})

	t.Run("existing bucket does not error", func(t *testing.T) {
		bucket := fmt.Sprintf("test-bucket-%d", time.Now().UnixNano())
		newTestClient(t, bucket)
		newTestClient(t, bucket) // idempotent — should not error
	})
}

func TestUpload(t *testing.T) {
	bucket := fmt.Sprintf("test-upload-%d", time.Now().UnixNano())
	c := newTestClient(t, bucket)

	tests := []struct {
		name    string
		ext     string
		content string
	}{
		{"html file", "html", "<html><body>test</body></html>"},
		{"markdown file", "md", "# Test\n\nSome text."},
		{"json file", "json", `{"url":"https://example.com","title":"Test","text":"hi"}`},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			jobID := uniqueJobID()

			path, err := c.Upload(context.Background(), jobID, tc.ext, []byte(tc.content))
			if err != nil {
				t.Fatalf("Upload: %v", err)
			}

			// Path must follow the ADR-001 convention: {bucket}/{jobID}.{ext}
			expectedPath := fmt.Sprintf("%s/%s.%s", bucket, jobID, tc.ext)
			if path != expectedPath {
				t.Errorf("path: got %q, want %q", path, expectedPath)
			}

			// Verify the object is retrievable using the underlying mc client.
			// Since the test is in the same package (package storage), we can
			// access the unexported mc field directly.
			objectName := fmt.Sprintf("%s.%s", jobID, tc.ext)
			obj, err := c.mc.GetObject(context.Background(), bucket, objectName, minio.GetObjectOptions{})
			if err != nil {
				t.Fatalf("GetObject: %v", err)
			}
			defer obj.Close()

			body, err := io.ReadAll(obj)
			if err != nil {
				t.Fatalf("reading object: %v", err)
			}
			if !strings.Contains(string(body), tc.content) {
				t.Errorf("stored content mismatch: got %q, want to contain %q", string(body), tc.content)
			}
		})
	}
}
