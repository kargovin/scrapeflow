// Package storage handles writing scrape results to MinIO object storage.
package storage

import (
	"bytes"
	"context"
	"fmt"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// Client wraps the MinIO SDK client and the target bucket name.
type Client struct {
	mc     *minio.Client
	bucket string
}

// New connects to MinIO and returns a storage Client.
// It also ensures the target bucket exists, creating it if necessary.
// This mirrors the Python API's startup behaviour in core/minio.py.
func New(endpoint, accessKey, secretKey, bucket string, secure bool) (*Client, error) {
	mc, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: secure,
	})
	if err != nil {
		return nil, fmt.Errorf("connecting to MinIO at %s: %w", endpoint, err)
	}

	// Ensure the bucket exists — idempotent, same logic as the Python API.
	ctx := context.Background()
	exists, err := mc.BucketExists(ctx, bucket)
	if err != nil {
		return nil, fmt.Errorf("checking bucket %q: %w", bucket, err)
	}
	if !exists {
		if err := mc.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil {
			return nil, fmt.Errorf("creating bucket %q: %w", bucket, err)
		}
	}

	return &Client{mc: mc, bucket: bucket}, nil
}

// Upload writes the scraped page to {bucket}/history/{artifactID}/scrape.{ext} and
// returns the full object path (e.g. "scrapeflow-results/history/abc123/scrape.html").
// This path is included in the result event so the API can store it in job_runs.result_path.
//
// Objects are keyed on the row that produced the execution and named by the producing
// stage (ADR-011 §1, §3). The stage segment is load-bearing rather than decorative: a
// flat history/{artifactID}.{ext} would collide with the LLM worker's output, which
// hardcodes a .json extension, so an output_format=json job's extraction would
// overwrite its own scraped page.
//
// There is no latest/ write (ADR-011 §4). It was write-only across the entire
// codebase — three workers wrote it and nothing ever read it — and dropping it also
// removes a round-trip here, because the history object used to be produced by
// CopyObject *from* latest/ rather than written directly.
func (c *Client) Upload(ctx context.Context, artifactID, ext string, data []byte) (string, error) {
	objectName := fmt.Sprintf("history/%s/scrape.%s", artifactID, ext)

	// bytes.NewReader wraps a byte slice to implement io.Reader,
	// which is what MinIO's PutObject expects. Think of it as io.BytesIO() in Python.
	reader := bytes.NewReader(data)

	_, err := c.mc.PutObject(ctx, c.bucket, objectName, reader, int64(len(data)),
		minio.PutObjectOptions{
			ContentType: contentType(ext),
		},
	)
	if err != nil {
		return "", fmt.Errorf("uploading %s to MinIO: %w", objectName, err)
	}

	// Return the full path including bucket name — matches what the Python
	// result consumer stores in job_runs.result_path.
	return fmt.Sprintf("%s/%s", c.bucket, objectName), nil
}

// contentType maps file extensions to MIME types for MinIO metadata.
func contentType(ext string) string {
	switch ext {
	case "html":
		return "text/html; charset=utf-8"
	case "md":
		return "text/markdown; charset=utf-8"
	case "json":
		return "application/json"
	default:
		return "application/octet-stream"
	}
}
