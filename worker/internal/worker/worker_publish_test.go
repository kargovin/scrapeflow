// Unit tests for publishResult — no external dependencies required.
// These run without the integration build tag.
package worker

import (
	"errors"
	"testing"

	"github.com/nats-io/nats.go"
)

// mockJS implements jetStreamClient for testing.
// publishErr controls whether Publish returns an error.
type mockJS struct {
	publishErr   error
	publishCalls int
}

func (m *mockJS) PullSubscribe(_ string, _ string, _ ...nats.SubOpt) (*nats.Subscription, error) {
	return nil, nil
}

func (m *mockJS) Publish(_ string, _ []byte, _ ...nats.PubOpt) (*nats.PubAck, error) {
	m.publishCalls++
	if m.publishErr != nil {
		return nil, m.publishErr
	}
	return &nats.PubAck{}, nil
}

func TestPublishResult_Success(t *testing.T) {
	js := &mockJS{}
	w := &Worker{js: js}

	err := w.publishResult(resultMessage{JobID: "job-1", Status: "completed", MinIOPath: "bucket/job-1.html"})
	if err != nil {
		t.Fatalf("expected nil error, got: %v", err)
	}
	if js.publishCalls != 1 {
		t.Fatalf("expected 1 Publish call, got %d", js.publishCalls)
	}
}

func TestPublishResult_PublishError(t *testing.T) {
	publishErr := errors.New("nats: connection lost")
	js := &mockJS{publishErr: publishErr}
	w := &Worker{js: js}

	err := w.publishResult(resultMessage{JobID: "job-2", Status: "completed"})
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !errors.Is(err, publishErr) {
		t.Fatalf("expected wrapped publishErr, got: %v", err)
	}
}

func TestPublishResult_MarshalNeverFails(t *testing.T) {
	// resultMessage only contains strings — json.Marshal cannot fail on it.
	// This test documents that assumption: if it ever fails, something changed.
	js := &mockJS{}
	w := &Worker{js: js}

	err := w.publishResult(resultMessage{}) // zero-value struct
	if err != nil {
		t.Fatalf("marshal of zero-value resultMessage should never fail, got: %v", err)
	}
}
