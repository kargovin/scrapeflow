// Package worker implements the NATS JetStream consumer loop.
// It subscribes to scrapeflow.jobs.run.http, executes each scrape job,
// and publishes results to scrapeflow.jobs.result — per ADR-002.
package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/http-worker/internal/formatter"
	"github.com/kargovin/scrapeflow/http-worker/internal/storage"
)

// NATS subject and stream constants — must match ADR-002 and the Python constants.py.
// These are not configurable: they are part of the worker contract.
const (
	jobsRunSubject    = "scrapeflow.jobs.run.http"
	jobsResultSubject = "scrapeflow.jobs.result"
	durableName       = "go-worker"
)

// jobMessage is the incoming message shape published by the API (ADR-002 §3).
// Go's json.Unmarshal maps JSON keys to struct fields via `json:"..."` tags.
// This is the Go equivalent of a Pydantic model used only for parsing.
type jobMessage struct {
	JobID        string `json:"job_id"`
	RunID        string `json:"run_id"`
	URL          string `json:"url"`
	OutputFormat string `json:"output_format"`
}

// resultMessage is the outgoing message shape published back to the API (ADR-002 §3).
// Omitempty means the field is omitted from JSON if it is the zero value (empty string / 0).
type resultMessage struct {
	JobID         string `json:"job_id"`
	RunID         string `json:"run_id"`
	Status        string `json:"status"`
	MinIOPath     string `json:"minio_path,omitempty"`
	NATSStreamSeq uint64 `json:"nats_stream_seq,omitempty"`
	Error         string `json:"error,omitempty"`
}

// jetStreamClient is the subset of nats.JetStreamContext used by Worker.
// Using a narrow interface keeps Worker testable without a real NATS server.
type jetStreamClient interface {
	PullSubscribe(subj, durable string, opts ...nats.SubOpt) (*nats.Subscription, error)
	Publish(subj string, data []byte, opts ...nats.PubOpt) (*nats.PubAck, error)
}

// Worker holds the dependencies needed to process scrape jobs.
type Worker struct {
	js      jetStreamClient
	fetcher *fetcher.Fetcher
	storage *storage.Client
}

// New creates a Worker with the given dependencies.
// This is the idiomatic Go constructor pattern — a New() function that
// returns a pointer to the struct. No classes, no __init__.
func New(js nats.JetStreamContext, f *fetcher.Fetcher, s *storage.Client) *Worker {
	return &Worker{js: js, fetcher: f, storage: s}
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
		sub.Unsubscribe() //nolint:errcheck // best-effort cleanup on shutdown
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
				w.handleMessage(ctx, m)
			}(msg)
		}
	}
}

// handleMessage implements the full ADR-002 job lifecycle:
//  1. Parse message
//  2. Publish "running" progress event (with nats_stream_seq)
//  3. Fetch URL
//  4. Format output
//  5. Upload to MinIO (latest/ + history/)
//  6. Publish "completed" or "failed" result event
//  7. Ack the NATS message (only after MinIO write succeeds)
func (w *Worker) handleMessage(ctx context.Context, msg *nats.Msg) {
	// --- Step 1: Parse the incoming job message ---
	var job jobMessage
	if err := json.Unmarshal(msg.Data, &job); err != nil {
		slog.Error("Malformed job message, discarding", "error", err, "data", string(msg.Data))
		if err := msg.Ack(); err != nil {
			slog.Error("Failed to ack malformed message", "error", err)
		}
		return
	}

	slog.Info("Received job", "job_id", job.JobID, "run_id", job.RunID, "url", job.URL, "format", job.OutputFormat)

	// --- Step 2: Publish "running" progress event (ADR-002 §3) ---
	// nats_stream_seq is stored by the result consumer on job_runs.nats_stream_seq.
	// The MaxDeliver advisory subscriber (Step 22) uses it to identify stalled runs —
	// NATS advisory messages carry only stream_seq, no job_id or run_id.
	runningMsg := resultMessage{
		JobID:  job.JobID,
		RunID:  job.RunID,
		Status: "running",
	}
	if meta, err := msg.Metadata(); err == nil {
		runningMsg.NATSStreamSeq = meta.Sequence.Stream
	}
	if err := w.publishResult(runningMsg); err != nil {
		slog.Error("Failed to publish 'running' result", "job_id", job.JobID, "run_id", job.RunID, "error", err)
	}

	// --- Steps 3–5: Fetch, format, upload ---
	minioPath, err := w.processJob(ctx, &job)
	if err != nil {
		slog.Error("Job failed", "job_id", job.JobID, "run_id", job.RunID, "error", err)
		if pubErr := w.publishResult(resultMessage{
			JobID:  job.JobID,
			RunID:  job.RunID,
			Status: "failed",
			Error:  err.Error(),
		}); pubErr != nil {
			slog.Error("Failed to publish 'failed' result", "job_id", job.JobID, "run_id", job.RunID, "error", pubErr)
			if nakErr := msg.NakWithDelay(30 * time.Second); nakErr != nil {
				slog.Error("Failed to nak message", "job_id", job.JobID, "error", nakErr)
			}
			return
		}
		// Ack even on failure — the result event already told the API it failed.
		// Not acking would redeliver, but the scrape already failed (e.g. site down).
		if ackErr := msg.Ack(); ackErr != nil {
			slog.Error("Failed to ack message after failed job", "job_id", job.JobID, "error", ackErr)
		}
		return
	}

	// --- Step 6: Publish "completed" result event ---
	if err := w.publishResult(resultMessage{
		JobID:     job.JobID,
		RunID:     job.RunID,
		Status:    "completed",
		MinIOPath: minioPath,
	}); err != nil {
		slog.Error("Failed to publish 'completed' result", "job_id", job.JobID, "run_id", job.RunID, "error", err)
		if nakErr := msg.NakWithDelay(30 * time.Second); nakErr != nil {
			slog.Error("Failed to nak message", "job_id", job.JobID, "error", nakErr)
		}
		return
	}

	// --- Step 7: Ack after MinIO write succeeds (ADR-002 §6) ---
	// If the worker crashes before this line, NATS redelivers the message.
	// Acking here means: "I have durably stored the result; stop tracking this message."
	if err := msg.Ack(); err != nil {
		slog.Error("Failed to ack message", "job_id", job.JobID, "run_id", job.RunID, "error", err)
	}
	slog.Info("Job completed", "job_id", job.JobID, "run_id", job.RunID, "minio_path", minioPath)
}

// processJob runs the fetch → format → upload pipeline and returns the MinIO history path.
func (w *Worker) processJob(ctx context.Context, job *jobMessage) (string, error) {
	fetchResult, err := w.fetcher.Fetch(ctx, job.URL)
	if err != nil {
		return "", fmt.Errorf("fetch failed: %w", err)
	}

	formatted, ext, err := formatter.Format(fetchResult.Body, job.OutputFormat, fetchResult.FinalURL)
	if err != nil {
		return "", fmt.Errorf("format failed: %w", err)
	}

	minioPath, err := w.storage.Upload(ctx, job.JobID, ext, formatted)
	if err != nil {
		return "", fmt.Errorf("upload failed: %w", err)
	}

	return minioPath, nil
}

// publishResult serializes and publishes a result message to scrapeflow.jobs.result.
func (w *Worker) publishResult(result resultMessage) error {
	data, err := json.Marshal(result)
	if err != nil {
		slog.Error("Failed to marshal result", "job_id", result.JobID, "error", err)
		return err
	}
	if _, err := w.js.Publish(jobsResultSubject, data); err != nil {
		slog.Error("Failed to publish result", "job_id", result.JobID, "error", err)
		return err
	}
	return nil
}
