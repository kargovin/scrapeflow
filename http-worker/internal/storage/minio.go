// Package storage handles writing scrape results to MinIO object storage.
package storage

import (
	"bytes"
	"context"
	"fmt"
	"time"

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

// Upload writes data to MinIO at the path {bucket}/{jobID}.{ext}
// and returns the full object path (e.g. "scrapeflow-results/abc123.html").
// This path is included in the result event so the API can store it in jobs.result_path.
func (c *Client) Upload(ctx context.Context, jobID, ext string, data []byte) (string, error) {

	// {bucket}/latest/{job_id}.{extension}
	objectNameLatest := fmt.Sprintf("latest/%s.%s", jobID, ext)

	// bytes.NewReader wraps a byte slice to implement io.Reader,
	// which is what MinIO's PutObject expects. Think of it as io.BytesIO() in Python.
	reader := bytes.NewReader(data)

	_, err := c.mc.PutObject(ctx, c.bucket, objectNameLatest, reader, int64(len(data)),
		minio.PutObjectOptions{
			ContentType: contentType(ext),
		},
	)
	if err != nil {
		return "", fmt.Errorf("uploading %s to MinIO: %w", objectNameLatest, err)
	}

	// Creating a history path with timestamp allows us to keep old results without overwriting.
	objectNameHistory := fmt.Sprintf("history/%s/%d.%s", jobID, time.Now().Unix(), ext)
	_, err = c.mc.CopyObject(ctx, minio.CopyDestOptions{
		Bucket: c.bucket,
		Object: objectNameHistory,
	}, minio.CopySrcOptions{
		Bucket: c.bucket,
		Object: objectNameLatest,
	})
	if err != nil {
		return "", fmt.Errorf("copying %s to history path in MinIO: %w", objectNameLatest, err)
	}

	// Return the full path including bucket name — matches what the Python
	// result consumer stores in jobs.result_path.
	return fmt.Sprintf("%s/%s", c.bucket, objectNameHistory), nil
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
