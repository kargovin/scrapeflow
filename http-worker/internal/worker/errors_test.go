// Unit tests for the transient/terminal classifier (UF-003 3a).
// Pure logic — no NATS, MinIO, or network required.
package worker

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
)

// wrapUpload mirrors what processJob does to a MinIO upload error before it reaches
// classify: fmt.Errorf("upload failed: %w", err) inside an *uploadError.
func wrapUpload(err error) error {
	return &uploadError{err: fmt.Errorf("upload failed: %w", err)}
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
			want: transient,
		},
		{
			name: "bare net.OpError from upload is transient",
			err:  wrapUpload(&net.OpError{Op: "read", Err: errors.New("connection reset by peer")}),
			want: transient,
		},
		{
			name: "MinIO 5xx (SlowDown) is transient",
			err:  wrapUpload(minio.ErrorResponse{Code: "SlowDown"}),
			want: transient,
		},
		{
			name: "MinIO 5xx (InternalError) is transient",
			err:  wrapUpload(minio.ErrorResponse{Code: "InternalError"}),
			want: transient,
		},
		{
			name: "MinIO ServiceUnavailable is transient",
			err:  wrapUpload(minio.ErrorResponse{Code: "ServiceUnavailable"}),
			want: transient,
		},
		{
			name: "MinIO caller error (NoSuchBucket) is terminal",
			err:  wrapUpload(minio.ErrorResponse{Code: "NoSuchBucket"}),
			want: terminal,
		},
		{
			name: "MinIO AccessDenied is terminal",
			err:  wrapUpload(minio.ErrorResponse{Code: "AccessDenied"}),
			want: terminal,
		},
		{
			name: "unknown upload error is terminal (fail closed)",
			err:  wrapUpload(errors.New("something we do not recognise")),
			want: terminal,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := classify(tc.err); got != tc.want {
				t.Errorf("classify: got %q, want %q", got, tc.want)
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
			if got := classify(tc.err); got != terminal {
				t.Errorf("classify: got %q, want %q", got, terminal)
			}
		})
	}
}

func TestRetryDelay(t *testing.T) {
	base := 5 * time.Second
	max := 60 * time.Second

	tests := []struct {
		attempt int
		want    time.Duration
	}{
		{0, 5 * time.Second},  // clamped up to attempt 1
		{1, 5 * time.Second},  // base
		{2, 10 * time.Second}, // 2*base
		{3, 20 * time.Second}, // 4*base
		{4, 40 * time.Second}, // 8*base
		{5, 60 * time.Second}, // 16*base capped at max
		{99, 60 * time.Second},
	}

	for _, tc := range tests {
		t.Run(fmt.Sprintf("attempt=%d", tc.attempt), func(t *testing.T) {
			if got := retryDelay(tc.attempt, base, max); got != tc.want {
				t.Errorf("retryDelay(%d): got %v, want %v", tc.attempt, got, tc.want)
			}
		})
	}
}
