package activity

import (
	"context"

	"github.com/fernet/fernet-go"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/http-worker/internal/robots"
	"github.com/kargovin/scrapeflow/http-worker/internal/scrape"
)

// TaskQueue and ScrapeName are part of the worker contract: the workflow schedules ScrapeName on
// TaskQueue. Not env-configurable.
const (
	TaskQueue  = "scrape-http"
	ScrapeName = "Scrape"
)

// ApplicationError types returned by Scrape. Every failure except ErrStorageTransient is
// non-retryable.
const (
	ErrInvalidInput     = "InvalidInput"
	ErrProxy            = "ProxyError"
	ErrRobotsDisallowed = "RobotsDisallowed"
	ErrScrapeFailed     = "ScrapeFailed"
	ErrStorageTransient = "StorageTransient"
)

type Activities struct {
	fetcher        *fetcher.Fetcher
	storage        scrape.Uploader
	credentialsKey *fernet.Key
}

func New(f *fetcher.Fetcher, s scrape.Uploader, credKey *fernet.Key) *Activities {
	return &Activities{fetcher: f, storage: s, credentialsKey: credKey}
}

// Scrape is the Temporal counterpart of worker.handleMessage — the same job lifecycle, one attempt:
//  1. Validate the input (ADR-011)
//  2. Resolve per-job fetcher (proxy transport if credentials.encrypted_proxy_url is set)
//  3. Enforce robots.txt if options.respect_robots is true
//  4. Fetch URL, format output, upload to MinIO (history/{artifact_id}/scrape.{ext}) — scrape.Run,
//     heartbeating at each stage
//  5. Return the stored object, or classify the failure
//
// handleMessage's other steps have no equivalent here: the workflow mirrors "running" (its step 4),
// and the return value replaces the result publish and the ack/nak (steps 8–9). Retries belong to
// the caller's RetryPolicy: a transient MinIO fault is returned as a retryable error, everything
// else as non-retryable. Error messages match what the NATS path reports.
func (a *Activities) Scrape(ctx context.Context, in ScrapeInput) (ScrapeOutput, error) {
	logger := activity.GetLogger(ctx)

	// --- Step 1: Validate the input ---
	// encoding/json enforces nothing, so a JSON null artifact_id arrives as "" (ADR-011 §5).
	if err := in.Validate(); err != nil {
		return ScrapeOutput{}, temporal.NewNonRetryableApplicationError(err.Error(), ErrInvalidInput, nil)
	}

	logger.Info("Received job", "artifact_id", in.ArtifactID, "url", in.URL, "format", in.OutputFormat)

	// --- Step 2: Resolve per-job fetcher ---
	// Default to the worker's shared fetcher (no proxy).
	// If credentials.encrypted_proxy_url is set, decrypt it and create a one-off fetcher
	// with a proxy transport. Decryption or URL parse failure is a config error —
	// fail immediately, do not retry.
	f := a.fetcher
	if in.Credentials != nil && in.Credentials.EncryptedProxyURL != "" {
		plaintext := fernet.VerifyAndDecrypt([]byte(in.Credentials.EncryptedProxyURL), 0, []*fernet.Key{a.credentialsKey})
		if plaintext == nil {
			return ScrapeOutput{}, temporal.NewNonRetryableApplicationError("proxy_decryption_failed", ErrProxy, nil)
		}
		pf, err := a.fetcher.WithProxy(string(plaintext))
		if err != nil {
			return ScrapeOutput{}, temporal.NewNonRetryableApplicationError("malformed_proxy_url: "+err.Error(), ErrProxy, nil)
		}
		f = pf
		logger.Info("Using proxy for job", "artifact_id", in.ArtifactID)
	}

	// --- Step 3: robots.txt enforcement ---
	// Fetched directly — never via the job proxy (spec §3.4).
	// Fetch failure is treated as no restrictions (proceed).
	if in.Options != nil && in.Options.RespectRobots {
		disallowed, err := robots.IsDisallowed(ctx, in.URL)
		if err != nil {
			logger.Warn("robots.txt check error, proceeding", "artifact_id", in.ArtifactID, "error", err)
		}
		if disallowed {
			return ScrapeOutput{}, temporal.NewNonRetryableApplicationError("robots_txt_disallowed", ErrRobotsDisallowed, nil)
		}
	}

	// --- Step 4: Fetch, format, upload ---
	// Each stage heartbeats, so a caller that sets a heartbeat_timeout detects a stuck attempt.
	res, err := scrape.Run(ctx, f, a.storage, in.ArtifactID, in.URL, in.OutputFormat,
		func(stage string) { activity.RecordHeartbeat(ctx, stage) })

	// --- Step 5: Classify the failure, or return the stored object ---
	// UF-003 3a: only a transient MinIO write fault is worth retrying — the fetch + format already
	// succeeded. A dead site, a format bug or a MinIO caller error is terminal. Temporal owns the
	// retry schedule; there is no in-activity retry loop.
	if err != nil {
		if scrape.Classify(err) == scrape.Transient {
			return ScrapeOutput{}, temporal.NewApplicationError(err.Error(), ErrStorageTransient)
		}
		return ScrapeOutput{}, temporal.NewNonRetryableApplicationError(err.Error(), ErrScrapeFailed, nil)
	}

	logger.Info("Scrape completed", "artifact_id", in.ArtifactID, "path", res.Path, "size", res.Size)
	return ScrapeOutput{
		Result:      StoredObject{Path: res.Path, Size: res.Size},
		ContentHash: res.ContentHash,
	}, nil
}
