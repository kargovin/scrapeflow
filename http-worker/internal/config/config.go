// Package config reads worker configuration from environment variables.
// Think of this as the Go equivalent of pydantic-settings' BaseSettings —
// but explicit: every field is read manually from os.Getenv.
package config

import (
	"fmt"
	"os"
	"runtime"
	"strconv"
)

// Config holds all runtime configuration for the worker.
// Fields map directly to environment variables set in Docker Compose.
type Config struct {
	// WorkerMode selects the entry point: ModeNATS (default) or ModeTemporal.
	WorkerMode string // WORKER_MODE

	// NATS — required in nats mode only
	NATSUrl        string // NATS_URL — e.g. nats://nats:4222
	NATSMaxDeliver int    // NATS_MAX_DELIVER — max redelivery attempts before giving up

	// MinIO
	MinIOEndpoint  string // MINIO_ENDPOINT — e.g. minio:9000
	MinIOAccessKey string // MINIO_ACCESS_KEY
	MinIOSecretKey string // MINIO_SECRET_KEY
	MinIOBucket    string // MINIO_BUCKET — defaults to scrapeflow-results
	MinIOSecure    bool   // MINIO_SECURE — false for local dev (no TLS)

	// Credentials
	CredentialsEncryptionKey string // CREDENTIALS_ENCRYPTION_KEY — Fernet key for proxy/cookies in NATS messages

	// HTTP fetcher
	FetchTimeoutSecs int // FETCH_TIMEOUT_SECS — per-URL HTTP timeout

	// Temporal — used in temporal mode only
	TemporalAddress   string // TEMPORAL_ADDRESS — e.g. temporal:7233
	TemporalNamespace string // TEMPORAL_NAMESPACE

	// Worker Runtime
	WorkerPoolSize int // WORKER_POOL_SIZE — number of concurrent jobs to process
}

const (
	ModeNATS     = "nats"
	ModeTemporal = "temporal"
)

// Load reads configuration from environment variables.
// It returns an error if any required variable is missing.
func Load() (*Config, error) {
	mode := envStr("WORKER_MODE", ModeNATS)
	if mode != ModeNATS && mode != ModeTemporal {
		return nil, fmt.Errorf("WORKER_MODE must be %q or %q, got %q", ModeNATS, ModeTemporal, mode)
	}

	natsURL := os.Getenv("NATS_URL")
	if mode == ModeNATS && natsURL == "" {
		return nil, fmt.Errorf("NATS_URL is required")
	}

	minioEndpoint := os.Getenv("MINIO_ENDPOINT")
	if minioEndpoint == "" {
		return nil, fmt.Errorf("MINIO_ENDPOINT is required")
	}

	minioAccessKey := os.Getenv("MINIO_ACCESS_KEY")
	if minioAccessKey == "" {
		return nil, fmt.Errorf("MINIO_ACCESS_KEY is required")
	}

	minioSecretKey := os.Getenv("MINIO_SECRET_KEY")
	if minioSecretKey == "" {
		return nil, fmt.Errorf("MINIO_SECRET_KEY is required")
	}

	credentialsKey := os.Getenv("CREDENTIALS_ENCRYPTION_KEY")
	if credentialsKey == "" {
		return nil, fmt.Errorf("CREDENTIALS_ENCRYPTION_KEY is required")
	}

	return &Config{
		WorkerMode: mode,

		NATSUrl:        natsURL,
		NATSMaxDeliver: envInt("NATS_MAX_DELIVER", 3),

		MinIOEndpoint:  minioEndpoint,
		MinIOAccessKey: minioAccessKey,
		MinIOSecretKey: minioSecretKey,
		MinIOBucket:    envStr("MINIO_BUCKET", "scrapeflow-results"),
		MinIOSecure:    envBool("MINIO_SECURE", false),

		CredentialsEncryptionKey: credentialsKey,

		TemporalAddress:   envStr("TEMPORAL_ADDRESS", "localhost:7233"),
		TemporalNamespace: envStr("TEMPORAL_NAMESPACE", "scrapeflow"),

		FetchTimeoutSecs: envInt("FETCH_TIMEOUT_SECS", 30),
		WorkerPoolSize:   envInt("WORKER_POOL_SIZE", runtime.NumCPU()),
	}, nil
}

// envStr returns the env var value or a default if unset.
func envStr(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

// envInt parses an env var as an integer, returning a default if unset or invalid.
func envInt(key string, defaultVal int) int {
	v := os.Getenv(key)
	if v == "" {
		return defaultVal
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return defaultVal
	}
	return n
}

// envBool parses an env var as a boolean ("true"/"1" → true), returning a default if unset.
func envBool(key string, defaultVal bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return defaultVal
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return defaultVal
	}
	return b
}
