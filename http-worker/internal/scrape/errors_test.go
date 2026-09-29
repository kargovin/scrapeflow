// Unit tests for the transient/terminal classifier (UF-003 3a).
// Pure logic — no NATS, MinIO, or network required.
package scrape

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"testing"

	"github.com/minio/minio-go/v7"
)

// wrapUpload mirrors what Run does to a MinIO upload error before it reaches
// Classify: fmt.Errorf("upload failed: %w", err) inside an *UploadError.
func wrapUpload(err error) error {
	return &UploadError{err: fmt.Errorf("upload failed: %w", err)}
}

func TestClassify_UploadFaults(t *testing.T) {
	// A realistic "MinIO down" shape from minio-go: *url.Error → *net.OpError →
	// syscall connection-refused. Both *url.Error and *net.OpError satisfy net.Error.
	connRefused := &url.Error{
		Op:  "Put",
		URL: "http://minio:9000/scrapeflow-results/latest/job.html",
		Err: &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("connect: connection refused")},
	}

	tests := []struct {
		name string
		err  error
		want string
	}{
		{
			name: "MinIO unreachable (connection refused) is transient",
			err:  wrapUpload(connRefused),
			want: Transient,
		},
		{
			name: "bare net.OpError from upload is transient",
			err:  wrapUpload(&net.OpError{Op: "read", Err: errors.New("connection reset by peer")}),
			want: Transient,
		},
		{
			name: "MinIO 5xx (SlowDown) is transient",
			err:  wrapUpload(minio.ErrorResponse{Code: "SlowDown"}),
			want: Transient,
		},
		{
			name: "MinIO 5xx (InternalError) is transient",
			err:  wrapUpload(minio.ErrorResponse{Code: "InternalError"}),
			want: Transient,
		},
		{
			name: "MinIO ServiceUnavailable is transient",
			err:  wrapUpload(minio.ErrorResponse{Code: "ServiceUnavailable"}),
			want: Transient,
		},
		{
			name: "MinIO caller error (NoSuchBucket) is terminal",
			err:  wrapUpload(minio.ErrorResponse{Code: "NoSuchBucket"}),
			want: Terminal,
		},
		{
			name: "MinIO AccessDenied is terminal",
			err:  wrapUpload(minio.ErrorResponse{Code: "AccessDenied"}),
			want: Terminal,
		},
		{
			name: "unknown upload error is terminal (fail closed)",
			err:  wrapUpload(errors.New("something we do not recognise")),
			want: Terminal,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := Classify(tc.err); got != tc.want {
				t.Errorf("Classify: got %q, want %q", got, tc.want)
			}
		})
	}
}

// TestClassify_NonUploadErrorsAreTerminal is the crux of the Go-specific port: a
// network error is transient ONLY when it came from the upload step. The same
// net.Error raised by the fetcher against a dead target site must stay terminal —
// unlike the Python workers, where a connection error can only ever come from MinIO.
func TestClassify_NonUploadErrorsAreTerminal(t *testing.T) {
	deadSite := &url.Error{
		Op:  "Get",
		URL: "http://dead.example.com",
		Err: &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("connect: connection refused")},
	}

	tests := []struct {
		name string
		err  error
	}{
		{"fetch net error (dead site) is NOT transient", fmt.Errorf("fetch failed: %w", deadSite)},
		{"bare net error without upload wrapper is terminal", deadSite},
		{"format error is terminal", fmt.Errorf("format failed: %w", errors.New("bad html"))},
		{"MinIO 5xx code but not from upload step is terminal", minio.ErrorResponse{Code: "SlowDown"}},
		{"plain error is terminal", errors.New("boom")},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := Classify(tc.err); got != Terminal {
				t.Errorf("Classify: got %q, want %q", got, Terminal)
			}
		})
	}
}
