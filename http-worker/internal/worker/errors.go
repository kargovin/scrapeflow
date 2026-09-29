// NATS redelivery backoff for transient failures. The transient/terminal classifier itself lives in
// internal/scrape, shared with the Temporal activity.

package worker

import "time"

const (
	// Exponential backoff bounds for naking a transient MinIO fault. Mirror the
	// playwright worker defaults (playwright_retry_base/max_delay_seconds): attempt
	// 1 waits 5s, attempt 2 waits 10s, capped at 60s. The attempt cap itself is the
	// consumer's NATS_MAX_DELIVER (3), so the cap rarely bites in practice.
	transientBaseDelay = 5 * time.Second
	transientMaxDelay  = 60 * time.Second
)

// retryDelay is the exponential backoff for a nak, in wall-clock duration.
//
// attempt is msg.Metadata().NumDelivered — 1 on first delivery — so the first retry
// waits base, the second 2*base, and so on, capped at max.
func retryDelay(attempt int, base, max time.Duration) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	d := base * time.Duration(int64(1)<<(attempt-1))
	if d > max || d <= 0 { // d <= 0 guards against shift overflow at large attempts
		return max
	}
	return d
}
