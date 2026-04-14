// Package formatter converts raw HTML bytes into the requested output format.
// Supported formats match ADR-001: "html", "markdown", "json".
package formatter

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	md "github.com/JohannesKaufmann/html-to-markdown"
	"golang.org/x/net/html"
)

// JSONOutput is the structure returned when output_format == "json".
// It is serialized to JSON and stored in MinIO.
type JSONOutput struct {
	URL   string `json:"url"`
	Title string `json:"title"`
	Text  string `json:"text"`
}

// Format converts rawHTML into the requested output format.
// Returns the formatted bytes and the file extension to use for MinIO storage.
// finalURL is the URL after redirects — used in JSON output.
func Format(rawHTML []byte, outputFormat string, finalURL string) ([]byte, string, error) {
	switch outputFormat {
	case "html":
		// Pass-through: store raw HTML as-is.
		return rawHTML, "html", nil

	case "markdown":
		// Use the html-to-markdown library to convert.
		// md.NewConverter("", true, nil) means: no domain, enable commonmark, default options.
		converter := md.NewConverter("", true, nil)
		markdown, err := converter.ConvertBytes(rawHTML)
		if err != nil {
			return nil, "", fmt.Errorf("html-to-markdown conversion failed: %w", err)
		}
		return []byte(markdown), "md", nil

	case "json":
		title, text, err := extractTitleAndText(rawHTML)
		if err != nil {
			return nil, "", fmt.Errorf("extracting title/text from HTML: %w", err)
		}
		output := JSONOutput{URL: finalURL, Title: title, Text: text}
		// json.Marshal serializes the struct to JSON bytes.
		// This is the Go equivalent of json.dumps() in Python.
		jsonBytes, err := json.Marshal(output)
		if err != nil {
			return nil, "", fmt.Errorf("marshaling JSON output: %w", err)
		}
		return jsonBytes, "json", nil

	default:
		return nil, "", fmt.Errorf("unknown output_format %q", outputFormat)
	}
}

// extractTitleAndText parses the HTML tree and extracts the <title> tag content
// and all visible text nodes (i.e. text not inside <script> or <style> tags).
func extractTitleAndText(rawHTML []byte) (title string, text string, err error) {
	// html.Parse builds a parse tree from the raw bytes.
	// This is Go's equivalent of BeautifulSoup(html, "html.parser").
	doc, err := html.Parse(bytes.NewReader(rawHTML))
	if err != nil {
		return "", "", err
	}

	var titleBuf strings.Builder
	var textBuf strings.Builder

	// traverse is a recursive function that walks the HTML node tree.
	// In Python you'd use soup.find_all() or .descendants; in Go we
	// walk the tree manually with a recursive function.
	var traverse func(*html.Node)
	traverse = func(n *html.Node) {
		if n.Type == html.ElementNode {
			// Skip script and style elements — their text is code, not content.
			if n.Data == "script" || n.Data == "style" {
				return
			}
			// Capture the <title> tag content.
			if n.Data == "title" && n.FirstChild != nil {
				titleBuf.WriteString(strings.TrimSpace(n.FirstChild.Data))
			}
		}

		// Collect text nodes (actual visible text in the page).
		if n.Type == html.TextNode {
			t := strings.TrimSpace(n.Data)
			if t != "" {
				textBuf.WriteString(t)
				textBuf.WriteString(" ")
			}
		}

		// Recurse into children — Go has no built-in tree traversal,
		// so we follow FirstChild and NextSibling pointers manually.
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			traverse(c)
		}
	}
	traverse(doc)

	return titleBuf.String(), strings.TrimSpace(textBuf.String()), nil
}
