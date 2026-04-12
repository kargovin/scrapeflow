package formatter

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestFormat(t *testing.T) {
	// sampleHTML is a minimal but realistic HTML page used across several cases.
	sampleHTML := `<!DOCTYPE html>
<html>
<head>
  <title>Hello World</title>
  <style>body { color: red; }</style>
</head>
<body>
  <p>Visible paragraph.</p>
  <script>var x = 1;</script>
  <p>Another paragraph.</p>
</body>
</html>`

	tests := []struct {
		name         string
		rawHTML      string
		outputFormat string
		finalURL     string
		wantExt      string
		wantErrMsg   string // non-empty means we expect an error containing this string
		check        func(t *testing.T, got []byte)
	}{
		{
			name:         "html passthrough",
			rawHTML:      sampleHTML,
			outputFormat: "html",
			finalURL:     "https://example.com",
			wantExt:      "html",
			check: func(t *testing.T, got []byte) {
				if string(got) != sampleHTML {
					t.Errorf("expected raw HTML passthrough, got different bytes")
				}
			},
		},
		{
			name:         "markdown converts html",
			rawHTML:      sampleHTML,
			outputFormat: "markdown",
			finalURL:     "https://example.com",
			wantExt:      "md",
			check: func(t *testing.T, got []byte) {
				s := string(got)
				if strings.Contains(s, "<html>") || strings.Contains(s, "<body>") {
					t.Errorf("markdown output should not contain HTML tags, got: %s", s)
				}
				if strings.TrimSpace(s) == "" {
					t.Errorf("markdown output should not be empty")
				}
			},
		},
		{
			name:         "json has url title text fields",
			rawHTML:      sampleHTML,
			outputFormat: "json",
			finalURL:     "https://example.com/page",
			wantExt:      "json",
			check: func(t *testing.T, got []byte) {
				var out JSONOutput
				if err := json.Unmarshal(got, &out); err != nil {
					t.Fatalf("json output is not valid JSON: %v", err)
				}
				if out.URL != "https://example.com/page" {
					t.Errorf("url: got %q, want %q", out.URL, "https://example.com/page")
				}
				if out.Title != "Hello World" {
					t.Errorf("title: got %q, want %q", out.Title, "Hello World")
				}
				if !strings.Contains(out.Text, "Visible paragraph") {
					t.Errorf("text should contain visible text, got: %q", out.Text)
				}
			},
		},
		{
			name:         "json strips script and style content",
			rawHTML:      sampleHTML,
			outputFormat: "json",
			finalURL:     "https://example.com",
			wantExt:      "json",
			check: func(t *testing.T, got []byte) {
				var out JSONOutput
				if err := json.Unmarshal(got, &out); err != nil {
					t.Fatalf("json output is not valid JSON: %v", err)
				}
				// Script and style text must not appear in the extracted text.
				if strings.Contains(out.Text, "var x = 1") {
					t.Errorf("text should not contain script content, got: %q", out.Text)
				}
				if strings.Contains(out.Text, "color: red") {
					t.Errorf("text should not contain style content, got: %q", out.Text)
				}
			},
		},
		{
			name:         "json with no title tag yields empty title",
			rawHTML:      `<html><body><p>No title here.</p></body></html>`,
			outputFormat: "json",
			finalURL:     "https://example.com",
			wantExt:      "json",
			check: func(t *testing.T, got []byte) {
				var out JSONOutput
				if err := json.Unmarshal(got, &out); err != nil {
					t.Fatalf("json output is not valid JSON: %v", err)
				}
				if out.Title != "" {
					t.Errorf("title should be empty, got %q", out.Title)
				}
				if !strings.Contains(out.Text, "No title here") {
					t.Errorf("text should contain body content, got: %q", out.Text)
				}
			},
		},
		{
			name:         "unknown format returns error",
			rawHTML:      sampleHTML,
			outputFormat: "pdf",
			finalURL:     "https://example.com",
			wantErrMsg:   "pdf",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, ext, err := Format([]byte(tc.rawHTML), tc.outputFormat, tc.finalURL)

			if tc.wantErrMsg != "" {
				if err == nil {
					t.Fatalf("expected error containing %q, got nil", tc.wantErrMsg)
				}
				if !strings.Contains(err.Error(), tc.wantErrMsg) {
					t.Errorf("error %q does not contain %q", err.Error(), tc.wantErrMsg)
				}
				return
			}

			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if ext != tc.wantExt {
				t.Errorf("ext: got %q, want %q", ext, tc.wantExt)
			}
			if tc.check != nil {
				tc.check(t, got)
			}
		})
	}
}
