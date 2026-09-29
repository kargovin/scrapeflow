package activity

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestScrapeInputValidate(t *testing.T) {
	cases := []struct {
		name    string
		payload string
		wantErr string
	}{
		{"valid", `{"artifact_id":"a","url":"https://example.com","output_format":"html"}`, ""},
		{"null artifact_id", `{"artifact_id":null,"url":"https://example.com","output_format":"html"}`, "missing artifact_id"},
		{"absent artifact_id", `{"url":"https://example.com","output_format":"html"}`, "missing artifact_id"},
		{"unknown format", `{"artifact_id":"a","url":"https://example.com","output_format":"pdf"}`, "unknown output_format"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var in ScrapeInput
			if err := json.Unmarshal([]byte(tc.payload), &in); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			err := in.Validate()
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("want error containing %q, got %v", tc.wantErr, err)
			}
		})
	}
}

func TestScrapeOutputOmitsNilSlices(t *testing.T) {
	out := ScrapeOutput{Result: StoredObject{Path: "b/k", Size: 1}, ContentHash: "00c0ffee12345678"}

	data, err := json.Marshal(out)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if strings.Contains(string(data), "null") {
		t.Fatalf("nil slice marshalled as null: %s", data)
	}
}
