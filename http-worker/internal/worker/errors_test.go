// Unit tests for the NATS redelivery backoff.
package worker

import (
	"fmt"
	"testing"
	"time"
)

func TestRetryDelay(t *testing.T) {
	base := 5 * time.Second
	max := 60 * time.Second

	tests := []struct {
		attempt int
		want    time.Duration
	}{
		{0, 5 * time.Second},  // clamped up to attempt 1
		{1, 5 * time.Second},  // base
		{2, 10 * time.Second}, // 2*base
		{3, 20 * time.Second}, // 4*base
		{4, 40 * time.Second}, // 8*base
		{5, 60 * time.Second}, // 16*base capped at max
		{99, 60 * time.Second},
	}

	for _, tc := range tests {
		t.Run(fmt.Sprintf("attempt=%d", tc.attempt), func(t *testing.T) {
			if got := retryDelay(tc.attempt, base, max); got != tc.want {
				t.Errorf("retryDelay(%d): got %v, want %v", tc.attempt, got, tc.want)
			}
		})
	}
}
