package scrape

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/kargovin/scrapeflow/http-worker/internal/fetcher"
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

func TestContentHashMatchesPython(t *testing.T) {
	// Expected values from Python: xxhash.xxh64(data).hexdigest().
	cases := map[string]string{
		"":                   "ef46db3751d8e999",
		"<html>hello</html>": "d71537cb6515e9e1",
		"scrapeflow-1":       "0644d29c1e64fde5", // leading zero: %x would print 15 characters
	}
	for in, want := range cases {
		if got := ContentHash([]byte(in)); got != want {
			t.Errorf("ContentHash(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRunReportsPathSizeAndHash(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("<html><body>hi</body></html>"))
	}))
	defer srv.Close()

	up := &fakeUploader{}
	var stages []string
	res, err := Run(context.Background(), fetcher.New(5), up, "art-1", srv.URL, "html",
		func(s string) { stages = append(stages, s) })
	if err != nil {
		t.Fatalf("Run: %v", err)
	}

	if res.Path != "bucket/history/art-1/scrape.html" {
		t.Errorf("path: %q", res.Path)
	}
	if res.Size != int64(len(up.data)) {
		t.Errorf("size: got %d, uploaded %d bytes", res.Size, len(up.data))
	}
	if res.ContentHash != ContentHash(up.data) {
		t.Errorf("content hash is not of the uploaded bytes")
	}
	if !reflect.DeepEqual(stages, []string{StageFetch, StageUpload}) {
		t.Errorf("stages: %v", stages)
	}
}

func TestRunWrapsUploadFailures(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("<html></html>"))
	}))
	defer srv.Close()

	_, err := Run(context.Background(), fetcher.New(5), &fakeUploader{err: errors.New("boom")},
		"art-1", srv.URL, "html", nil)

	var ue *UploadError
	if !errors.As(err, &ue) {
		t.Fatalf("want *UploadError, got %T: %v", err, err)
	}
}
