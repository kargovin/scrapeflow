// Package worker implements the NATS JetStream consumer loop.
// It subscribes to scrapeflow.jobs.run, executes each scrape job,
// and publishes results to scrapeflow.jobs.result — per ADR-001.
package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/kargovin/scrapeflow/worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/worker/internal/formatter"
	"github.com/kargovin/scrapeflow/worker/internal/storage"
)

// NATS subject and stream constants — must match ADR-001 and the Python constants.py.
// These are not configurable: they are part of the worker contract.
const (
	jobsRunSubject    = "scrapeflow.jobs.run"
	jobsResultSubject = "scrapeflow.jobs.result"
	durableName       = "go-worker"
)

// jobMessage is the incoming message shape published by the API (ADR-001 §3).
// Go's json.Unmarshal maps JSON keys to struct fields via `json:"..."` tags.
// This is the Go equivalent of a Pydantic model used only for parsing.
type jobMessage struct {
	JobID        string `json:"job_id"`
	URL          string `json:"url"`
	OutputFormat string `json:"output_format"`
}

// resultMessage is the outgoing message shape published back to the API (ADR-001 §3).
// Omitempty means the field is omitted from JSON if it is the zero value (empty string).
type resultMessage struct {
	JobID     string `json:"job_id"`
	Status    string `json:"status"`
	MinIOPath string `json:"minio_path,omitempty"`
	Error     string `json:"error,omitempty"`
}

// jetStreamClient is the subset of nats.JetStreamContext used by Worker.
// Using a narrow interface keeps Worker testable without a real NATS server.
type jetStreamClient interface {
	Subscribe(subj string, cb nats.MsgHandler, opts ...nats.SubOpt) (*nats.Subscription, error)
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

// Run subscribes to the jobs.run subject and processes messages in a loop.
// It blocks until ctx is cancelled (i.e. the process is shutting down).
func (w *Worker) Run(ctx context.Context, maxDeliver int) error {
	// Create a push-based subscription with a durable consumer.
	// A durable consumer (named "go-worker") persists in NATS so that if the
	// worker restarts, it picks up unacknowledged messages from where it left off.
	// This is the Go equivalent of the Python result consumer's durable subscription.
	handler := func(msg *nats.Msg) {
		w.handleMessage(ctx, msg)
	}
	sub, err := w.js.Subscribe(
		jobsRunSubject,
		handler,
		nats.Durable(durableName),
		nats.AckExplicit(),          // We ack manually — never auto-ack
		nats.DeliverAll(),           // Start from the beginning if no prior state
		nats.MaxDeliver(maxDeliver), // NATS retries up to defined times before giving up
		nats.AckWait(5*time.Minute), // Allow up to 5 min per job before redelivery
	)
	if err != nil {
		return fmt.Errorf("subscribing to %s: %w", jobsRunSubject, err)
	}
	defer sub.Unsubscribe()

	log.Printf("Worker subscribed to %s, waiting for jobs...", jobsRunSubject)

	// Block here until the context is cancelled (e.g. SIGINT/SIGTERM).
	// The NATS subscription processes messages on a background goroutine managed
	// by the NATS library — w.handleMessage is called for each message.
	<-ctx.Done()
	log.Println("Worker shutting down")
	return nil
}

// handleMessage is the callback invoked by the NATS library for each message.
// It implements the full ADR-001 job lifecycle:
//  1. Parse message
//  2. Publish "running" progress event
//  3. Fetch URL
//  4. Format output
//  5. Upload to MinIO
//  6. Publish "completed" or "failed" result event
//  7. Ack the NATS message (only after MinIO write succeeds)
func (w *Worker) handleMessage(ctx context.Context, msg *nats.Msg) {
	// --- Step 1: Parse the incoming job message ---
	var job jobMessage
	// json.Unmarshal is Go's equivalent of json.loads() + Pydantic validation.
	// It fills the struct fields from the JSON bytes.
	if err := json.Unmarshal(msg.Data, &job); err != nil {
		log.Printf("Malformed job message, discarding: %v — data: %s", err, msg.Data)
		// Ack malformed messages to prevent infinite redelivery (same logic as Python).
		msg.Ack()
		return
	}

	log.Printf("Received job %s: url=%s format=%s", job.JobID, job.URL, job.OutputFormat)

	// --- Step 2: Publish "running" progress event (ADR-001 §3) ---
	// This tells the API result consumer to set job.status = "running" in Postgres.
	err := w.publishResult(resultMessage{
		JobID:  job.JobID,
		Status: "running",
	})
	if err != nil {
		log.Printf("Failed to publish 'running' result for job %s: %v", job.JobID, err)
	}

	// --- Steps 3–6: Fetch, format, upload, publish outcome ---
	// processJob does the real work. On failure it returns an error string.
	minioPath, err := w.processJob(ctx, &job)
	if err != nil {
		log.Printf("Job %s failed: %v", job.JobID, err)
		err := w.publishResult(resultMessage{
			JobID:  job.JobID,
			Status: "failed",
			Error:  err.Error(),
		})
		if err != nil {
			log.Printf("Failed to publish 'failed' result for job %s: %v", job.JobID, err)
			// We'll send a NAK here to trigger redelivery, hoping the next attempt succeeds in publishing the failure result.
			msg.NakWithDelay(30 * time.Second)
			return
		}
		// Ack even on failure — the result event already told the API it failed.
		// Not acking would cause NATS to redeliver, but the scrape already failed
		// (e.g. site is down), so redelivery would likely fail again.
		msg.Ack()
		return
	}

	// --- Step 6 (success): Publish "completed" result event ---
	err = w.publishResult(resultMessage{
		JobID:     job.JobID,
		Status:    "completed",
		MinIOPath: minioPath,
	})
	if err != nil {
		log.Printf("Failed to publish 'completed' result for job %s: %v", job.JobID, err)
		msg.NakWithDelay(30 * time.Second)
		return
	}

	// --- Step 7: Ack the NATS message AFTER MinIO write succeeds (ADR-001 §5) ---
	// If the worker crashed before this line, NATS would redeliver the message.
	// Acking here means: "I have durably stored the result; you can stop tracking this message."
	msg.Ack()
	log.Printf("Job %s completed, result at %s", job.JobID, minioPath)
}

// processJob runs the fetch → format → upload pipeline and returns the MinIO path.
func (w *Worker) processJob(ctx context.Context, job *jobMessage) (string, error) {
	// Step 3: Fetch the URL
	fetchResult, err := w.fetcher.Fetch(ctx, job.URL)
	if err != nil {
		return "", fmt.Errorf("fetch failed: %w", err)
	}

	// Step 4: Format the output
	formatted, ext, err := formatter.Format(fetchResult.Body, job.OutputFormat, fetchResult.FinalURL)
	if err != nil {
		return "", fmt.Errorf("format failed: %w", err)
	}

	// Step 5: Upload to MinIO
	minioPath, err := w.storage.Upload(ctx, job.JobID, ext, formatted)
	if err != nil {
		return "", fmt.Errorf("upload failed: %w", err)
	}

	return minioPath, nil
}

// publishResult serializes and publishes a result message to scrapeflow.jobs.result.
// Failures here are logged but do not abort the job — the NATS message will still be acked.
func (w *Worker) publishResult(result resultMessage) error {
	data, err := json.Marshal(result)
	if err != nil {
		log.Printf("Failed to marshal result for job %s: %v", result.JobID, err)
		return err
	}
	if _, err := w.js.Publish(jobsResultSubject, data); err != nil {
		log.Printf("Failed to publish result for job %s: %v", result.JobID, err)
		return err
	}
	return nil
}
