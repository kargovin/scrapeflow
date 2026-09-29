// Package activity holds the Temporal activity side of the http-worker.
package activity

import "errors"

// Twin of the API's Scrape activity contracts (api/app/workflows/activities/contracts.py).
// Fields the http-worker does not use (cookies, actions, playwright_options) are not defined;
// encoding/json ignores them.

type Credentials struct {
	EncryptedProxyURL string `json:"encrypted_proxy_url"`
}

type ScrapeOptions struct {
	RespectRobots bool `json:"respect_robots"`
}

type ScrapeInput struct {
	ArtifactID   string         `json:"artifact_id"`
	URL          string         `json:"url"`
	OutputFormat string         `json:"output_format"`
	Credentials  *Credentials   `json:"credentials"`
	Options      *ScrapeOptions `json:"options"`
}

// Validate enforces what the API's model enforces on its side; encoding/json checks none of it.
func (in ScrapeInput) Validate() error {
	// A JSON null decodes to "" without error.
	if in.ArtifactID == "" {
		return errors.New("missing artifact_id")
	}
	switch in.OutputFormat {
	case "html", "markdown", "json":
	default:
		return errors.New("unknown output_format: " + in.OutputFormat)
	}
	return nil
}

type StoredObject struct {
	Path string `json:"path"`
	Size int64  `json:"size"`
}

type ScrapeOutput struct {
	Result StoredObject `json:"result"`
	// 16 lowercase hex characters: format with %016x — plain %x drops leading zeros.
	ContentHash string `json:"content_hash"`
	// omitempty: a nil slice marshals to null, which the API's list fields reject.
	Warnings    []string       `json:"warnings,omitempty"`
	Screenshots []StoredObject `json:"screenshots,omitempty"`
}
