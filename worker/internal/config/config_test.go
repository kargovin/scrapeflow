package config

import (
	"testing"
)

// setRequiredVars sets all 4 required environment variables using t.Setenv,
// which automatically restores the original values after the test.
func setRequiredVars(t *testing.T) {
	t.Helper()
	t.Setenv("NATS_URL", "nats://localhost:4222")
	t.Setenv("MINIO_ENDPOINT", "localhost:9000")
	t.Setenv("MINIO_ACCESS_KEY", "testkey")
	t.Setenv("MINIO_SECRET_KEY", "testsecret")
}

func TestLoad_RequiredVars(t *testing.T) {
	t.Run("all required vars present", func(t *testing.T) {
		setRequiredVars(t)

		cfg, err := Load()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if cfg.NATSUrl != "nats://localhost:4222" {
			t.Errorf("NATSUrl: got %q", cfg.NATSUrl)
		}
		if cfg.MinIOEndpoint != "localhost:9000" {
			t.Errorf("MinIOEndpoint: got %q", cfg.MinIOEndpoint)
		}
	})

	t.Run("NATS_URL missing returns error", func(t *testing.T) {
		setRequiredVars(t)
		t.Setenv("NATS_URL", "") // override to empty

		_, err := Load()
		if err == nil {
			t.Fatal("expected error when NATS_URL is missing")
		}
	})

	t.Run("MINIO_ENDPOINT missing returns error", func(t *testing.T) {
		setRequiredVars(t)
		t.Setenv("MINIO_ENDPOINT", "")

		_, err := Load()
		if err == nil {
			t.Fatal("expected error when MINIO_ENDPOINT is missing")
		}
	})

	t.Run("MINIO_ACCESS_KEY missing returns error", func(t *testing.T) {
		setRequiredVars(t)
		t.Setenv("MINIO_ACCESS_KEY", "")

		_, err := Load()
		if err == nil {
			t.Fatal("expected error when MINIO_ACCESS_KEY is missing")
		}
	})

	t.Run("MINIO_SECRET_KEY missing returns error", func(t *testing.T) {
		setRequiredVars(t)
		t.Setenv("MINIO_SECRET_KEY", "")

		_, err := Load()
		if err == nil {
			t.Fatal("expected error when MINIO_SECRET_KEY is missing")
		}
	})
}

func TestLoad_Defaults(t *testing.T) {
	t.Run("optional vars absent use defaults", func(t *testing.T) {
		setRequiredVars(t)
		// Explicitly clear optional vars so we test defaults.
		t.Setenv("MINIO_BUCKET", "")
		t.Setenv("MINIO_SECURE", "")
		t.Setenv("FETCH_TIMEOUT_SECS", "")
		t.Setenv("NATS_MAX_DELIVER", "")

		cfg, err := Load()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if cfg.MinIOBucket != "scrapeflow-results" {
			t.Errorf("MinIOBucket default: got %q, want %q", cfg.MinIOBucket, "scrapeflow-results")
		}
		if cfg.MinIOSecure != false {
			t.Errorf("MinIOSecure default: got %v, want false", cfg.MinIOSecure)
		}
		if cfg.FetchTimeoutSecs != 30 {
			t.Errorf("FetchTimeoutSecs default: got %d, want 30", cfg.FetchTimeoutSecs)
		}
		if cfg.NATSMaxDeliver != 3 {
			t.Errorf("NATSMaxDeliver default: got %d, want 3", cfg.NATSMaxDeliver)
		}
	})

	t.Run("FETCH_TIMEOUT_SECS parsed as int", func(t *testing.T) {
		setRequiredVars(t)
		t.Setenv("FETCH_TIMEOUT_SECS", "45")

		cfg, err := Load()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if cfg.FetchTimeoutSecs != 45 {
			t.Errorf("FetchTimeoutSecs: got %d, want 45", cfg.FetchTimeoutSecs)
		}
	})

	t.Run("MINIO_SECURE=true parsed as bool", func(t *testing.T) {
		setRequiredVars(t)
		t.Setenv("MINIO_SECURE", "true")

		cfg, err := Load()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if cfg.MinIOSecure != true {
			t.Errorf("MinIOSecure: got %v, want true", cfg.MinIOSecure)
		}
	})

	t.Run("invalid FETCH_TIMEOUT_SECS falls back to default", func(t *testing.T) {
		setRequiredVars(t)
		t.Setenv("FETCH_TIMEOUT_SECS", "not-a-number")

		cfg, err := Load()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if cfg.FetchTimeoutSecs != 30 {
			t.Errorf("FetchTimeoutSecs fallback: got %d, want 30", cfg.FetchTimeoutSecs)
		}
	})
}
