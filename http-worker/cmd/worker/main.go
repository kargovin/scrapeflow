// cmd/worker/main.go — ScrapeFlow Go HTTP scraper worker entry point.
// This wires together config → MinIO → fetcher, then the entry point WORKER_MODE selects:
// the NATS consumer loop, or a Temporal worker serving the Scrape activity.
package main

import (
	"context"
	"log"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/fernet/fernet-go"
	"github.com/nats-io/nats.go"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	tlog "go.temporal.io/sdk/log"
	tworker "go.temporal.io/sdk/worker"

	scrapeactivity "github.com/kargovin/scrapeflow/http-worker/internal/activity"
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
	log.Printf("Config loaded: mode=%s MinIO=%s bucket=%s", cfg.WorkerMode, cfg.MinIOEndpoint, cfg.MinIOBucket)

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

	// --- Decode the credentials encryption key (Fernet) ---
	credKey, err := fernet.DecodeKey(cfg.CredentialsEncryptionKey)
	if err != nil {
		log.Fatalf("Invalid CREDENTIALS_ENCRYPTION_KEY: %v", err)
	}

	// --- Create the HTTP fetcher ---
	fetch := fetcher.New(cfg.FetchTimeoutSecs)

	if cfg.WorkerMode == config.ModeTemporal {
		runTemporal(cfg, scrapeactivity.New(fetch, store, credKey))
		return
	}
	runNATS(cfg, fetch, store, credKey)
}

// runTemporal serves the Scrape activity on its task queue until SIGINT/SIGTERM, then lets in-flight
// activities finish for up to 20 s — under k8s's default 30 s grace period.
func runTemporal(cfg *config.Config, acts *scrapeactivity.Activities) {
	c, err := client.Dial(client.Options{
		HostPort:  cfg.TemporalAddress,
		Namespace: cfg.TemporalNamespace,
		Logger:    tlog.NewStructuredLogger(slog.Default()),
	})
	if err != nil {
		log.Fatalf("Temporal connect error: %v", err)
	}
	defer c.Close()

	w := tworker.New(c, scrapeactivity.TaskQueue, tworker.Options{
		MaxConcurrentActivityExecutionSize: cfg.WorkerPoolSize,
		WorkerStopTimeout:                  20 * time.Second,
		// Activities only: without this the SDK also polls the queue for workflow tasks.
		DisableWorkflowWorker: true,
	})
	w.RegisterActivityWithOptions(acts.Scrape, activity.RegisterOptions{Name: scrapeactivity.ScrapeName})

	slog.Info("Temporal worker started",
		"address", cfg.TemporalAddress, "namespace", cfg.TemporalNamespace,
		"task_queue", scrapeactivity.TaskQueue, "pool_size", cfg.WorkerPoolSize)
	if err := w.Run(tworker.InterruptCh()); err != nil {
		log.Fatalf("Temporal worker error: %v", err)
	}
	slog.Info("Temporal worker stopped")
}

func runNATS(cfg *config.Config, fetch *fetcher.Fetcher, store *storage.Client, credKey *fernet.Key) {
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

	// --- Wire the worker ---
	w := worker.New(js, fetch, store, credKey)

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
