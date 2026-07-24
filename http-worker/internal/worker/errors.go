// Transient vs terminal failure classification for the Go HTTP worker (UF-003 3a).
//
// Ported from playwright-worker/worker/errors.py (itself ported from the LLM worker).
// Same failure mode on all three workers: handleMessage used to Ack on *every*
// error, so a momentary infra blip failed a job permanently — the Ack preempts
// JetStream's redelivery, so the queue never gets the chance to retry. The expensive
// work (fetch + format) has already succeeded by the time we upload, so failing the
// job over a transient object-store fault throws that work away.
//
//   - terminal  — a dead target site, a bad proxy key, a format bug. Retrying cannot
//                 help and just re-fetches; a dead site is its own answer.
//   - transient — MinIO unreachable or overloaded. Retrying is very likely to succeed
//                 once the object store recovers.
//
// The Go exception surface differs from the Python workers in a way that matters.
// In Python a connection/network error can *only* come from MinIO (miniopy-async),
// so classifying by exception type is safe. In Go both the fetcher (net/http) and
// minio-go use the standard net stack, so a dead *target site* and a dead *MinIO*
// both surface as *net.OpError / *url.Error — a net error alone is ambiguous. We
// therefore only treat an error as a transient candidate when we know it came from
// the upload step, which handleMessage marks with *uploadError. Everything else is
// terminal by default — an error we do not recognise is not retried.

package worker

import (
	"errors"
	"net"
	"time"

	"github.com/minio/minio-go/v7"
)

const (
	transient = "transient"
	terminal  = "terminal"

	// Exponential backoff bounds for naking a transient MinIO fault. Mirror the
	// playwright worker defaults (playwright_retry_base/max_delay_seconds): attempt
	// 1 waits 5s, attempt 2 waits 10s, capped at 60s. The attempt cap itself is the
	// consumer's NATS_MAX_DELIVER (3), so the cap rarely bites in practice.
	transientBaseDelay = 5 * time.Second
	transientMaxDelay  = 60 * time.Second
)

// uploadError marks a failure that occurred while writing to MinIO, as opposed to
// fetching the target site or formatting the output. Only MinIO write faults are
// candidates for transient retry; handleMessage wraps the upload step's error in
// this so classify can tell a MinIO connection failure apart from an identical-looking
// net error raised by the fetcher against a dead site.
type uploadError struct {
	err error
}

func (e *uploadError) Error() string { return e.err.Error() }
func (e *uploadError) Unwrap() error { return e.err }

// transientS3Codes are minio-go S3 error codes that indicate load or a transient
// backend fault rather than a caller mistake (NoSuchBucket, AccessDenied, etc. stay
// terminal via the default). Mirrors the Python _TRANSIENT_S3_CODES set.
var transientS3Codes = map[string]struct{}{
	"InternalError":      {},
	"SlowDown":           {},
	"ServiceUnavailable": {},
	"RequestTimeout":     {},
}

// classify returns transient or terminal for an error returned by the job pipeline.
// Only a MinIO *write* fault (an *uploadError) can be transient; every other error —
// including a net error from the fetcher against a dead site — is terminal.
func classify(err error) string {
	var ue *uploadError
	if !errors.As(err, &ue) {
		return terminal
	}
	return classifyMinIO(ue.err)
}

// classifyMinIO classifies the underlying error from a MinIO upload.
//
// The non-obvious split (the same one that bit the LLM worker in 6ad95e3): "MinIO
// down" is a *different error class* from "MinIO returned a 5xx". Connection refused /
// reset / dial timeout surfaces as *net.OpError / *url.Error (both satisfy net.Error),
// carrying NO S3 code — so a code-only match would miss the literal down case. MinIO
// being reachable but faulting surfaces as minio.ErrorResponse with a .Code.
func classifyMinIO(err error) string {
	// MinIO unreachable at the network layer. minio-go wraps connection refused /
	// reset / dial timeout in *url.Error → *net.OpError, both of which satisfy
	// net.Error, so a single interface check catches them.
	var netErr net.Error
	if errors.As(err, &netErr) {
		return transient
	}

	// MinIO reachable but returned an error response. Only load/backend codes retry.
	var respErr minio.ErrorResponse
	if errors.As(err, &respErr) {
		if _, ok := transientS3Codes[respErr.Code]; ok {
			return transient
		}
	}

	return terminal
}

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
