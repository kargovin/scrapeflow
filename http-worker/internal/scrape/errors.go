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
// the upload step, which Run marks with *UploadError. Everything else is
// terminal by default — an error we do not recognise is not retried.

package scrape

import (
	"errors"
	"net"

	"github.com/minio/minio-go/v7"
)

const (
	Transient = "transient"
	Terminal  = "terminal"
)

// UploadError marks a failure that occurred while writing to MinIO, as opposed to
// fetching the target site or formatting the output. Only MinIO write faults are
// candidates for transient retry; Run wraps the upload step's error in this so
// Classify can tell a MinIO connection failure apart from an identical-looking
// net error raised by the fetcher against a dead site.
type UploadError struct {
	err error
}

func (e *UploadError) Error() string { return e.err.Error() }
func (e *UploadError) Unwrap() error { return e.err }

// transientS3Codes are minio-go S3 error codes that indicate load or a transient
// backend fault rather than a caller mistake (NoSuchBucket, AccessDenied, etc. stay
// terminal via the default). Mirrors the Python _TRANSIENT_S3_CODES set.
var transientS3Codes = map[string]struct{}{
	"InternalError":      {},
	"SlowDown":           {},
	"ServiceUnavailable": {},
	"RequestTimeout":     {},
}

// Classify returns Transient or Terminal for an error returned by Run.
// Only a MinIO *write* fault (an *UploadError) can be transient; every other error —
// including a net error from the fetcher against a dead site — is terminal.
func Classify(err error) string {
	var ue *UploadError
	if !errors.As(err, &ue) {
		return Terminal
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
		return Transient
	}

	// MinIO reachable but returned an error response. Only load/backend codes retry.
	var respErr minio.ErrorResponse
	if errors.As(err, &respErr) {
		if _, ok := transientS3Codes[respErr.Code]; ok {
			return Transient
		}
	}

	return Terminal
}
