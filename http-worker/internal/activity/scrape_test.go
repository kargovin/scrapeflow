package activity

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"github.com/fernet/fernet-go"
	"github.com/minio/minio-go/v7"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"

	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
	"github.com/kargovin/scrapeflow/http-worker/internal/scrape"
)

type fakeUploader struct {
	data []byte
	err  error
}

func (u *fakeUploader) Upload(_ context.Context, artifactID, ext string, data []byte) (string, error) {
	if u.err != nil {
		return "", u.err
	}
	u.data = data
	return fmt.Sprintf("bucket/history/%s/scrape.%s", artifactID, ext), nil
}

func site(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/robots.txt" {
			_, _ = w.Write([]byte("User-agent: *\nDisallow: /private\n"))
			return
		}
		_, _ = w.Write([]byte("<html><head><title>t</title></head><body>hello</body></html>"))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func testKey(t *testing.T) *fernet.Key {
	t.Helper()
	var k fernet.Key
	if err := k.Generate(); err != nil {
		t.Fatalf("generate key: %v", err)
	}
	return &k
}

// run executes Scrape by its registered name, as a workflow would.
func run(t *testing.T, acts *Activities, in ScrapeInput) (ScrapeOutput, []string, error) {
	t.Helper()
	var s testsuite.WorkflowTestSuite
	env := s.NewTestActivityEnvironment()
	env.RegisterActivityWithOptions(acts.Scrape, activity.RegisterOptions{Name: ScrapeName})

	var heartbeats []string
	env.SetOnActivityHeartbeatListener(func(_ *activity.Info, details converter.EncodedValues) {
		var stage string
		if err := details.Get(&stage); err == nil {
			heartbeats = append(heartbeats, stage)
		}
	})

	val, err := env.ExecuteActivity(ScrapeName, in)
	if err != nil {
		return ScrapeOutput{}, heartbeats, err
	}
	var out ScrapeOutput
	if err := val.Get(&out); err != nil {
		t.Fatalf("decode output: %v", err)
	}
	return out, heartbeats, nil
}

// appError asserts err is an ApplicationError of errType and reports whether it is retryable.
func appError(t *testing.T, err error, errType string) (retryable bool) {
	t.Helper()
	var ae *temporal.ApplicationError
	if !errors.As(err, &ae) {
		t.Fatalf("want *temporal.ApplicationError, got %T: %v", err, err)
	}
	if ae.Type() != errType {
		t.Fatalf("error type: got %q, want %q (%v)", ae.Type(), errType, err)
	}
	return !ae.NonRetryable()
}

func TestScrape_Success(t *testing.T) {
	srv := site(t)
	up := &fakeUploader{}

	out, heartbeats, err := run(t, New(fetcher.New(5), up, testKey(t)), ScrapeInput{
		ArtifactID: "art-1", URL: srv.URL, OutputFormat: "markdown",
	})
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}

	if out.Result.Path != "bucket/history/art-1/scrape.md" {
		t.Errorf("path: %q", out.Result.Path)
	}
	if out.Result.Size != int64(len(up.data)) || out.Result.Size == 0 {
		t.Errorf("size: got %d, uploaded %d bytes", out.Result.Size, len(up.data))
	}
	if out.ContentHash != scrape.ContentHash(up.data) {
		t.Errorf("content hash is not of the uploaded bytes")
	}
	// The SDK sends the first heartbeat at once and throttles the rest, so only the first is certain.
	if len(heartbeats) == 0 || heartbeats[0] != scrape.StageFetch {
		t.Errorf("heartbeats: %v", heartbeats)
	}
}

func TestScrape_TransientStorageFaultIsRetryable(t *testing.T) {
	srv := site(t)
	down := &url.Error{Op: "Put", URL: "http://minio:9000/b/k",
		Err: &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("connect: connection refused")}}

	_, _, err := run(t, New(fetcher.New(5), &fakeUploader{err: down}, testKey(t)), ScrapeInput{
		ArtifactID: "art-1", URL: srv.URL, OutputFormat: "html",
	})

	if !appError(t, err, ErrStorageTransient) {
		t.Error("a MinIO connection fault must be retryable")
	}
}

func TestScrape_TerminalStorageFaultIsNonRetryable(t *testing.T) {
	srv := site(t)

	_, _, err := run(t, New(fetcher.New(5), &fakeUploader{err: minio.ErrorResponse{Code: "AccessDenied"}}, testKey(t)),
		ScrapeInput{ArtifactID: "art-1", URL: srv.URL, OutputFormat: "html"})

	if appError(t, err, ErrScrapeFailed) {
		t.Error("AccessDenied must not be retried")
	}
}

func TestScrape_DeadSiteIsNonRetryable(t *testing.T) {
	// A dead site raises the same net error class as a dead MinIO; only the upload step's is transient.
	srv := httptest.NewServer(http.NotFoundHandler())
	deadURL := srv.URL
	srv.Close()
	up := &fakeUploader{}

	_, _, err := run(t, New(fetcher.New(5), up, testKey(t)), ScrapeInput{
		ArtifactID: "art-1", URL: deadURL, OutputFormat: "html",
	})

	if appError(t, err, ErrScrapeFailed) {
		t.Error("a dead target site must not be retried")
	}
	if up.data != nil {
		t.Error("nothing should be uploaded for a dead site")
	}
}

func TestScrape_InvalidInputIsNonRetryable(t *testing.T) {
	_, _, err := run(t, New(fetcher.New(5), &fakeUploader{}, testKey(t)), ScrapeInput{
		URL: "https://example.com", OutputFormat: "html",
	})

	if appError(t, err, ErrInvalidInput) {
		t.Error("invalid input must not be retried")
	}
}

func TestScrape_UndecryptableProxyIsNonRetryable(t *testing.T) {
	srv := site(t)

	_, _, err := run(t, New(fetcher.New(5), &fakeUploader{}, testKey(t)), ScrapeInput{
		ArtifactID: "art-1", URL: srv.URL, OutputFormat: "html",
		Credentials: &Credentials{EncryptedProxyURL: "not-a-fernet-token"},
	})

	if appError(t, err, ErrProxy) {
		t.Error("an undecryptable proxy must not be retried")
	}
}

func TestScrape_RobotsDisallowedIsNonRetryable(t *testing.T) {
	srv := site(t)
	up := &fakeUploader{}

	_, _, err := run(t, New(fetcher.New(5), up, testKey(t)), ScrapeInput{
		ArtifactID: "art-1", URL: srv.URL + "/private/page", OutputFormat: "html",
		Options: &ScrapeOptions{RespectRobots: true},
	})

	if appError(t, err, ErrRobotsDisallowed) {
		t.Error("a robots.txt block must not be retried")
	}
	if up.data != nil {
		t.Error("nothing should be uploaded when robots.txt disallows")
	}
}
