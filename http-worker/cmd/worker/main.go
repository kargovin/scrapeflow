// cmd/worker/main.go — ScrapeFlow Go HTTP scraper worker entry point.
// This wires together config → MinIO → fetcher, then serves the Scrape activity on its Temporal
// task queue.
package main

import (
	"log"
	"log/slog"
	"time"

	"github.com/fernet/fernet-go"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	tlog "go.temporal.io/sdk/log"
	tworker "go.temporal.io/sdk/worker"

	scrapeactivity "github.com/kargovin/scrapeflow/http-worker/internal/activity"
	"github.com/kargovin/scrapeflow/http-worker/internal/config"
	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/http-worker/internal/storage"
)

func main() {
	// --- Load configuration from environment variables ---
	cfg, err := config.Load()
	if err != nil {
		// log.Fatalf is like Python's sys.exit(1) after printing an error —
		// it logs the message and terminates the process immediately.
		log.Fatalf("Config error: %v", err)
	}
	log.Printf("Config loaded: MinIO=%s bucket=%s", cfg.MinIOEndpoint, cfg.MinIOBucket)

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

	runTemporal(cfg, scrapeactivity.New(fetch, store, credKey))
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
