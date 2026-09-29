// Package scrape is the fetch → format → upload pipeline, shared by the NATS worker and the
// Temporal activity.
package scrape

import (
	"context"
	"fmt"

	"github.com/cespare/xxhash/v2"

	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/http-worker/internal/formatter"
)

// Uploader is the subset of storage.Client the pipeline needs.
type Uploader interface {
	Upload(ctx context.Context, artifactID, ext string, data []byte) (string, error)
}

// Result describes the object the pipeline stored.
type Result struct {
	Path        string // "{bucket}/{key}"
	Size        int64
	ContentHash string
}

// Stage names passed to the progress callback.
const (
	StageFetch  = "fetch"
	StageUpload = "upload"
)

// Run fetches url, formats it as outputFormat and uploads it under artifactID. progress, if not nil,
// is called as each stage starts. An upload failure is returned as *UploadError.
func Run(
	ctx context.Context,
	f *fetcher.Fetcher,
	store Uploader,
	artifactID, url, outputFormat string,
	progress func(stage string),
) (Result, error) {
	if progress == nil {
		progress = func(string) {}
	}

	progress(StageFetch)
	fetchResult, err := f.Fetch(ctx, url)
	if err != nil {
		return Result{}, fmt.Errorf("fetch failed: %w", err)
	}

	formatted, ext, err := formatter.Format(fetchResult.Body, outputFormat, fetchResult.FinalURL)
	if err != nil {
		return Result{}, fmt.Errorf("format failed: %w", err)
	}

	progress(StageUpload)
	path, err := store.Upload(ctx, artifactID, ext, formatted)
	if err != nil {
		return Result{}, &UploadError{err: fmt.Errorf("upload failed: %w", err)}
	}

	return Result{Path: path, Size: int64(len(formatted)), ContentHash: ContentHash(formatted)}, nil
}

// ContentHash is xxh64 of data as 16 lowercase hex characters — the same value as Python's
// xxhash.xxh64(data).hexdigest(). %016x, not %x: %x drops leading zeros.
func ContentHash(data []byte) string {
	return fmt.Sprintf("%016x", xxhash.Sum64(data))
}
