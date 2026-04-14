// cmd/worker/main.go — ScrapeFlow Go scraper worker entry point.
// This wires together config → NATS → MinIO → fetcher → worker loop.
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/nats-io/nats.go"

	"github.com/kargovin/scrapeflow/http-worker/internal/config"
	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/http-worker/internal/storage"
	"github.com/kargovin/scrapeflow/http-worker/internal/worker"
)

func main() {
	// --- Load configuration from environment variables ---
	cfg, err := config.Load()
	if err != nil {
		// log.Fatalf is like Python's sys.exit(1) after printing an error —
		// it logs the message and terminates the process immediately.
		log.Fatalf("Config error: %v", err)
	}
	log.Printf("Config loaded: NATS=%s MinIO=%s bucket=%s", cfg.NATSUrl, cfg.MinIOEndpoint, cfg.MinIOBucket)

	// --- Connect to NATS JetStream ---
	// nats.Connect returns a *nats.Conn (the raw TCP connection to NATS).
	nc, err := nats.Connect(cfg.NATSUrl,
		// Reconnect automatically if the NATS server restarts.
		nats.MaxReconnects(-1),    // -1 means retry forever
		nats.ReconnectWait(2*1e9), // wait 2 seconds between attempts (in nanoseconds)
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			log.Printf("NATS disconnected: %v", err)
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			log.Println("NATS reconnected")
		}),
	)
	if err != nil {
		log.Fatalf("NATS connect error: %v", err)
	}
	defer nc.Drain() //nolint:errcheck // best-effort flush before exit

	// JetStream() returns the JetStream context from the base NATS connection.
	// JetStream is the persistent messaging layer on top of plain NATS pub/sub.
	js, err := nc.JetStream()
	if err != nil {
		log.Fatalf("JetStream context error: %v", err)
	}

	// Assert the SCRAPEFLOW stream exists — fail fast if it doesn't.
	// The stream is created by the nats-init Docker Compose service (ADR-001 §1).
	// If it doesn't exist, something is wrong with the infrastructure.
	if _, err := js.StreamInfo("SCRAPEFLOW"); err != nil {
		log.Fatalf("SCRAPEFLOW JetStream stream not found — is nats-init running? %v", err)
	}
	log.Println("SCRAPEFLOW stream confirmed")

	// --- Connect to MinIO ---
	store, err := storage.New(
		cfg.MinIOEndpoint,
		cfg.MinIOAccessKey,
		cfg.MinIOSecretKey,
		cfg.MinIOBucket,
		cfg.MinIOSecure,
	)
	if err != nil {
		log.Fatalf("MinIO connect error: %v", err)
	}
	log.Printf("MinIO connected: bucket=%s", cfg.MinIOBucket)

	// --- Create the HTTP fetcher ---
	fetch := fetcher.New(cfg.FetchTimeoutSecs)

	// --- Wire the worker ---
	w := worker.New(js, fetch, store)

	// --- Graceful shutdown via OS signal handling ---
	// context.WithCancel creates a context that we can cancel manually.
	// Passing this ctx to w.Run() means the worker loop stops when we cancel.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Make a channel that receives SIGINT (Ctrl+C) and SIGTERM (docker stop).
	// This is the standard Go pattern for process lifecycle management.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	// Run the signal handler in a separate goroutine (go keyword = spawn goroutine).
	// A goroutine is like a lightweight thread — much cheaper than an OS thread.
	// When the signal arrives, we cancel the context, which unblocks w.Run().
	go func() {
		sig := <-sigCh
		log.Printf("Received signal %s, shutting down...", sig)
		cancel()
	}()

	// --- Start the worker loop (blocks until ctx is cancelled) ---
	if err := w.Run(ctx, cfg.NATSMaxDeliver, cfg.WorkerPoolSize); err != nil {
		log.Fatalf("Worker error: %v", err)
	}
}
