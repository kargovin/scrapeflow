// Package worker implements the NATS JetStream consumer loop.
// It subscribes to scrapeflow.jobs.run.http, executes each scrape job,
// and publishes results to scrapeflow.jobs.result — per ADR-002.
package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/fernet/fernet-go"
	"github.com/nats-io/nats.go"

	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/http-worker/internal/formatter"
	"github.com/kargovin/scrapeflow/http-worker/internal/robots"
	"github.com/kargovin/scrapeflow/http-worker/internal/storage"
)

// NATS subject and stream constants — must match ADR-002 and the Python constants.py.
// These are not configurable: they are part of the worker contract.
const (
	jobsRunSubject    = "scrapeflow.jobs.run.http"
	jobsResultSubject = "scrapeflow.jobs.result"
	durableName       = "go-worker"

	// The wire version this worker speaks (ADR-011). Versions 1 and 2 keyed artifacts
	// on job_id, which two of the three dispatch lanes could not honestly supply.
	supportedSchemaVersion = 3
)

// Credentials carries per-job secrets from the API. Values are Fernet ciphertexts —
// decrypted in handleMessage using the worker's credentialsKey.
// Cookies are consumed by the Playwright worker only — not defined here.
type Credentials struct {
	EncryptedProxyURL string `json:"encrypted_proxy_url,omitempty"`
}

// Options carries per-job behavioural flags.
// Actions are consumed by the Playwright worker only — not defined here.
type Options struct {
	RespectRobots bool `json:"respect_robots"`
}

// CrawlContext is set only when this run was dispatched by the BFS coordinator (ADR-005).
// The HTTP worker passes it through to the result message so the coordinator can route it.
type CrawlContext struct {
	CrawlID     string `json:"crawl_id"`
	CrawlPageID string `json:"crawl_page_id"`
	Depth       int    `json:"depth"`
}

// ScrapeMessage is the schema_version 3 incoming message shape (ADR-011).
// Fields not used by the HTTP worker (llm_config, playwright_options, credentials.cookies,
// options.actions) are not defined here — json.Unmarshal silently ignores unknown fields.
type ScrapeMessage struct {
	SchemaVersion int `json:"schema_version"`

	// ArtifactID names what this execution's artifacts are keyed on: job_runs.id on
	// the job and batch lanes, crawl_pages.id on the crawl lane. The worker uses it
	// verbatim and never interprets it — it does not know which lane it is on.
	// Validated non-empty in handleMessage; see the note there for why.
	ArtifactID string `json:"artifact_id"`

	// RunID is the result-routing key, and is absent on the crawl lane, which creates
	// no job_runs row (ADR-011 §2). It is a *pointer* deliberately: a plain string
	// cannot distinguish "absent" from "present and empty", and treating the second
	// as the first is precisely the defaulting behaviour ADR-011 §5 forbids.
	RunID *string `json:"run_id"`

	URL          string        `json:"url"`
	OutputFormat string        `json:"output_format"`
	Engine       string        `json:"engine"`
	Credentials  *Credentials  `json:"credentials"`
	Options      *Options      `json:"options"`
	CrawlContext *CrawlContext `json:"crawl_context"`
}

// resultMessage is the outgoing message shape published back to the API (ADR-002 §3).
// Omitempty means the field is omitted from JSON if it is the zero value (empty string / 0).
type resultMessage struct {
	// job_id has left the wire (ADR-011 §2): the API reads it from the job_runs row it
	// already loads, so there is one source for it rather than two that could disagree.
	RunID         *string       `json:"run_id,omitempty"`
	Status        string        `json:"status"`
	Source        string        `json:"source,omitempty"`
	MinIOPath     string        `json:"minio_path,omitempty"`
	NATSStreamSeq uint64        `json:"nats_stream_seq,omitempty"`
	Error         string        `json:"error,omitempty"`
	CrawlContext  *CrawlContext `json:"crawl_context,omitempty"`
}

// jetStreamClient is the subset of nats.JetStreamContext used by Worker.
// Using a narrow interface keeps Worker testable without a real NATS server.
type jetStreamClient interface {
	PullSubscribe(subj, durable string, opts ...nats.SubOpt) (*nats.Subscription, error)
	Publish(subj string, data []byte, opts ...nats.PubOpt) (*nats.PubAck, error)
}

// storageClient is the subset of storage.Client used by Worker.
// The narrow interface lets unit tests inject a mock without a real MinIO connection.
type storageClient interface {
	Upload(ctx context.Context, artifactID, ext string, data []byte) (string, error)
}

// Worker holds the dependencies needed to process scrape jobs.
type Worker struct {
	js             jetStreamClient
	fetcher        *fetcher.Fetcher
	storage        storageClient
	credentialsKey *fernet.Key // decoded once at startup; used to decrypt proxy URL per message
}

// New creates a Worker with the given dependencies.
// This is the idiomatic Go constructor pattern — a New() function that
// returns a pointer to the struct. No classes, no __init__.
func New(js nats.JetStreamContext, f *fetcher.Fetcher, s *storage.Client, credKey *fernet.Key) *Worker {
	return &Worker{js: js, fetcher: f, storage: s, credentialsKey: credKey}
}

// Run subscribes to the jobs.run.http subject and processes messages in a pull loop.
// It blocks until ctx is cancelled (i.e. the process is shutting down).
func (w *Worker) Run(ctx context.Context, maxDeliver int, workerPoolSize int) error {
	// PullSubscribe creates a durable pull consumer.
	// A durable consumer persists in NATS so that if the worker restarts,
	// it picks up unacknowledged messages from where it left off.
	sub, err := w.js.PullSubscribe(
		jobsRunSubject,
		durableName,
		nats.MaxDeliver(maxDeliver),
	)
	if err != nil {
		return fmt.Errorf("subscribing to %s: %w", jobsRunSubject, err)
	}
	defer func() {
		sub.Drain() //nolint:errcheck // best-effort flush; does not delete the durable consumer
	}()
	slog.Info("Worker subscribed", "subject", jobsRunSubject, "pool_size", workerPoolSize)

	// sem is a buffered channel used as a semaphore to cap concurrent jobs.
	// Sending to sem acquires a slot; receiving from sem releases one.
	sem := make(chan struct{}, workerPoolSize)
	backoff := 2 * time.Second // NATS fetch error backoff, doubles on each non-timeout error

	for {
		// Check for shutdown before each fetch — ctx.Done() is closed on SIGTERM.
		select {
		case <-ctx.Done():
			slog.Info("Worker shutting down")
			return nil
		default:
		}

		available := cap(sem) - len(sem)
		if available == 0 {
			time.Sleep(100 * time.Millisecond)
			continue
		}

		// Fetch only as many messages as there are free worker slots.
		// Fetching more would start AckWait timers on messages we can't process yet,
		// causing spurious NATS redelivery.
		msgs, err := sub.Fetch(available, nats.MaxWait(5*time.Second))
		if err != nil {
			// nats.ErrTimeout fires every ~5s when the queue is empty — that is normal.
			// Any other error (connection lost, server gone) gets exponential backoff
			// to avoid a busy-loop burning 100% CPU while NATS is down.
			if err != nats.ErrTimeout {
				backoff = min(backoff*2, 30*time.Second)
				slog.Warn("NATS fetch error, backing off", "backoff", backoff, "error", err)
				time.Sleep(backoff)
			} else {
				backoff = 2 * time.Second // reset after a clean timeout
			}
			continue
		}
		backoff = 2 * time.Second // reset on successful fetch

		for _, msg := range msgs {
			sem <- struct{}{} // Acquire a slot
			go func(m *nats.Msg) {
				defer func() { <-sem }() // Release slot when the job finishes
				w.handleMessage(ctx, m, maxDeliver)
			}(msg)
		}
	}
}

// handleMessage implements the full ADR-002 job lifecycle:
//  1. Parse and validate the message (schema_version 3 — ADR-011)
//  2. Resolve per-job fetcher (proxy transport if credentials.proxy_url is set)
//  3. Enforce robots.txt if options.respect_robots is true (TODO: Step 14 — wire internal/robots)
//  4. Publish "running" progress event (with nats_stream_seq)
//  5. Fetch URL
//  6. Format output
//  7. Upload to MinIO (history/{artifact_id}/scrape.{ext})
//  8. Publish "completed" or "failed" result event
//  9. Ack the NATS message (only after MinIO write succeeds)
func (w *Worker) handleMessage(ctx context.Context, msg *nats.Msg, maxDeliver int) {
	// --- Step 1: Parse and validate the incoming job message (schema_version 3) ---
	var job ScrapeMessage
	if err := json.Unmarshal(msg.Data, &job); err != nil {
		rejectMessage(msg, "Malformed job message, discarding", "error", err)
		return
	}

	if err := validateMessage(&job); err != nil {
		rejectMessage(msg, "Invalid job message, discarding", "error", err)
		return
	}

	runID := ""
	if job.RunID != nil {
		runID = *job.RunID
	}

	slog.Info("Received job", "artifact_id", job.ArtifactID, "run_id", runID, "url", job.URL,
		"format", job.OutputFormat, "schema_version", job.SchemaVersion)

	// --- Step 2: Resolve per-job fetcher ---
	// Default to the worker's shared fetcher (no proxy).
	// If credentials.encrypted_proxy_url is set, decrypt it and create a one-off fetcher
	// with a proxy transport. Decryption or URL parse failure is a config error —
	// fail immediately, do not retry.
	f := w.fetcher
	if job.Credentials != nil && job.Credentials.EncryptedProxyURL != "" {
		plaintext := fernet.VerifyAndDecrypt([]byte(job.Credentials.EncryptedProxyURL), 0, []*fernet.Key{w.credentialsKey})
		if plaintext == nil {
			slog.Error("Failed to decrypt proxy URL, failing run", "artifact_id", job.ArtifactID, "run_id", runID)
			if pubErr := w.publishResult(resultMessage{
				RunID:        job.RunID,
				Status:       "failed",
				Source:       "scrape",
				Error:        "proxy_decryption_failed",
				CrawlContext: job.CrawlContext,
			}); pubErr != nil {
				slog.Error("Failed to publish decrypt-error result", "artifact_id", job.ArtifactID, "error", pubErr)
			}
			if ackErr := msg.Ack(); ackErr != nil {
				slog.Error("Failed to ack after decrypt error", "artifact_id", job.ArtifactID, "error", ackErr)
			}
			return
		}
		proxyURL := string(plaintext)
		pf, err := w.fetcher.WithProxy(proxyURL)
		if err != nil {
			slog.Error("Malformed proxy URL, failing run", "artifact_id", job.ArtifactID, "run_id", runID, "error", err)
			if pubErr := w.publishResult(resultMessage{
				RunID:        job.RunID,
				Status:       "failed",
				Source:       "scrape",
				Error:        "malformed_proxy_url: " + err.Error(),
				CrawlContext: job.CrawlContext,
			}); pubErr != nil {
				slog.Error("Failed to publish proxy-error result", "artifact_id", job.ArtifactID, "error", pubErr)
			}
			if ackErr := msg.Ack(); ackErr != nil {
				slog.Error("Failed to ack after proxy error", "artifact_id", job.ArtifactID, "error", ackErr)
			}
			return
		}
		f = pf
		slog.Info("Using proxy for job", "artifact_id", job.ArtifactID)
	}

	// --- Step 3: robots.txt enforcement ---
	// Fetched directly — never via the job proxy (spec §3.4).
	// Fetch failure is treated as no restrictions (proceed).
	if job.Options != nil && job.Options.RespectRobots {
		disallowed, err := robots.IsDisallowed(ctx, job.URL)
		if err != nil {
			slog.Warn("robots.txt check error, proceeding", "artifact_id", job.ArtifactID, "url", job.URL, "error", err)
		}
		if disallowed {
			slog.Info("robots.txt disallows URL, failing run", "artifact_id", job.ArtifactID, "url", job.URL)
			if pubErr := w.publishResult(resultMessage{
				RunID:        job.RunID,
				Status:       "failed",
				Source:       "scrape",
				Error:        "robots_txt_disallowed",
				CrawlContext: job.CrawlContext,
			}); pubErr != nil {
				slog.Error("Failed to publish robots-blocked result", "artifact_id", job.ArtifactID, "error", pubErr)
			}
			if ackErr := msg.Ack(); ackErr != nil {
				slog.Error("Failed to ack after robots block", "artifact_id", job.ArtifactID, "error", ackErr)
			}
			return
		}
	}

	// --- Step 4: Publish "running" progress event (ADR-002 §3) ---
	// nats_stream_seq is stored by the result consumer on job_runs.nats_stream_seq.
	// The MaxDeliver advisory subscriber (Step 22) uses it to identify stalled runs —
	// NATS advisory messages carry only stream_seq, no job_id or run_id.
	runningMsg := resultMessage{
		RunID:        job.RunID,
		Status:       "running",
		Source:       "scrape",
		CrawlContext: job.CrawlContext,
	}
	if meta, err := msg.Metadata(); err == nil {
		runningMsg.NATSStreamSeq = meta.Sequence.Stream
	}
	if err := w.publishResult(runningMsg); err != nil {
		slog.Error("Failed to publish 'running' result", "artifact_id", job.ArtifactID, "run_id", runID, "error", err)
	}

	// --- Steps 5–7: Fetch, format, upload ---
	minioPath, err := w.processJob(ctx, &job, f)
	if err != nil {
		// UF-003 3a. This branch used to publish "failed" + Ack on *every* error,
		// which made a transient MinIO write fault as permanent as a dead site —
		// the Ack preempts JetStream redelivery, so the queue never retried, even
		// though the fetch + format had already succeeded. Now a transient infra
		// fault is naked back for a bounded redelivery; only the final outcome is
		// published. Same posture as the playwright/LLM workers (see errors.go).
		attempt := 1
		if meta, merr := msg.Metadata(); merr == nil {
			attempt = int(meta.NumDelivered)
		}
		kind := classify(err)

		if kind == transient && attempt < maxDeliver {
			delay := retryDelay(attempt, transientBaseDelay, transientMaxDelay)
			slog.Warn("Transient failure, naking for redelivery",
				"artifact_id", job.ArtifactID, "run_id", runID,
				"attempt", attempt, "max_attempts", maxDeliver, "retry_in", delay, "error", err)
			// Deliberately no "failed" publish: the API's terminal-status guard
			// would lock the run as failed and then discard the retry's "completed".
			// Only the final attempt reports an outcome. Redelivery re-runs the whole
			// scrape — there is no partial-progress checkpoint.
			if nakErr := msg.NakWithDelay(delay); nakErr != nil {
				slog.Error("Failed to nak after transient failure", "artifact_id", job.ArtifactID, "error", nakErr)
			}
			return
		}

		// Terminal, or the last attempt of a transient failure. Report and ack —
		// redelivery cannot recover a dead site or a bad key, and we are out of retries.
		errText := err.Error()
		if kind == transient {
			errText = fmt.Sprintf("%s (gave up after %d attempts)", errText, attempt)
		}
		slog.Error("Job failed", "artifact_id", job.ArtifactID, "run_id", runID,
			"kind", kind, "attempt", attempt, "error", errText)
		if pubErr := w.publishResult(resultMessage{
			RunID:        job.RunID,
			Status:       "failed",
			Source:       "scrape",
			Error:        errText,
			CrawlContext: job.CrawlContext,
		}); pubErr != nil {
			slog.Error("Failed to publish 'failed' result", "artifact_id", job.ArtifactID, "run_id", runID, "error", pubErr)
			if nakErr := msg.NakWithDelay(30 * time.Second); nakErr != nil {
				slog.Error("Failed to nak message", "artifact_id", job.ArtifactID, "error", nakErr)
			}
			return
		}
		if ackErr := msg.Ack(); ackErr != nil {
			slog.Error("Failed to ack message after failed job", "artifact_id", job.ArtifactID, "error", ackErr)
		}
		return
	}

	// --- Step 8: Publish "completed" result event ---
	if err := w.publishResult(resultMessage{
		RunID:        job.RunID,
		Status:       "completed",
		Source:       "scrape",
		MinIOPath:    minioPath,
		CrawlContext: job.CrawlContext,
	}); err != nil {
		slog.Error("Failed to publish 'completed' result", "artifact_id", job.ArtifactID, "run_id", runID, "error", err)
		if nakErr := msg.NakWithDelay(30 * time.Second); nakErr != nil {
			slog.Error("Failed to nak message", "artifact_id", job.ArtifactID, "error", nakErr)
		}
		return
	}

	// --- Step 9: Ack after MinIO write succeeds (ADR-002 §6) ---
	// If the worker crashes before this line, NATS redelivers the message.
	// Acking here means: "I have durably stored the result; stop tracking this message."
	if err := msg.Ack(); err != nil {
		slog.Error("Failed to ack message", "artifact_id", job.ArtifactID, "run_id", runID, "error", err)
	}
	slog.Info("Job completed", "artifact_id", job.ArtifactID, "run_id", runID, "minio_path", minioPath)
}

// processJob runs the fetch → format → upload pipeline and returns the MinIO history path.
// f is the per-job fetcher — either the worker's default or a proxy-configured one.
func (w *Worker) processJob(ctx context.Context, job *ScrapeMessage, f *fetcher.Fetcher) (string, error) {
	fetchResult, err := f.Fetch(ctx, job.URL)
	if err != nil {
		return "", fmt.Errorf("fetch failed: %w", err)
	}

	formatted, ext, err := formatter.Format(fetchResult.Body, job.OutputFormat, fetchResult.FinalURL)
	if err != nil {
		return "", fmt.Errorf("format failed: %w", err)
	}

	minioPath, err := w.storage.Upload(ctx, job.ArtifactID, ext, formatted)
	if err != nil {
		// Wrap in *uploadError so handleMessage can tell a MinIO write fault (a
		// transient-retry candidate) apart from a net error raised by the fetcher
		// against a dead site (terminal). See errors.go.
		return "", &uploadError{err: fmt.Errorf("upload failed: %w", err)}
	}

	return minioPath, nil
}

// publishResult serializes and publishes a result message to scrapeflow.jobs.result.
func (w *Worker) publishResult(result resultMessage) error {
	data, err := json.Marshal(result)
	if err != nil {
		slog.Error("Failed to marshal result", "status", result.Status, "error", err)
		return err
	}
	if _, err := w.js.Publish(jobsResultSubject, data); err != nil {
		slog.Error("Failed to publish result", "status", result.Status, "error", err)
		return err
	}
	return nil
}

// validateMessage enforces the one rule that made BUG-005 Path B possible: a missing
// identifier fails loudly, and is never defaulted (ADR-011 §5).
//
// This is the exact spot that bug lived. encoding/json unmarshals a JSON null into a
// string field as "" and returns no error — a no-op per the spec Go implements, not a
// fault — so the worker proceeded with an empty job id and wrote every batch item of
// every tenant to one shared object. A hard failure on the Python workers became
// silent cross-tenant corruption here purely from the two languages' defaults on a
// missing value. Checking explicitly is what closes the class; fixing only the field
// it happened to hit would not.
func validateMessage(job *ScrapeMessage) error {
	if job.SchemaVersion != supportedSchemaVersion {
		return fmt.Errorf("unsupported schema_version %d (want %d)",
			job.SchemaVersion, supportedSchemaVersion)
	}
	if job.ArtifactID == "" {
		return errors.New("missing artifact_id")
	}
	// Absent run_id is valid — the crawl lane creates no job_runs row. Present but
	// empty is a producer bug, and a plain string field could not tell them apart.
	if job.RunID != nil && *job.RunID == "" {
		return errors.New("empty run_id")
	}
	return nil
}

// rejectMessage logs a structurally invalid message and acks it. Redelivery cannot
// repair a producer bug, so the message is dropped — but loudly, which is the whole
// point: the alternative this replaces was proceeding with a defaulted value and
// corrupting storage in silence (ADR-011 §5).
func rejectMessage(msg *nats.Msg, reason string, args ...any) {
	slog.Error(reason, append(args, "data", string(msg.Data))...)
	if err := msg.Ack(); err != nil {
		slog.Error("Failed to ack rejected message", "error", err)
	}
}
