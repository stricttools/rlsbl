package release

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
)

// selfdocFile is selfdoc's configuration, which declares the version the
// documentation describes.
const selfdocFile = "selfdoc.json"

// span is a byte range of a document.
type span struct{ from, to int }

// bumpSelfdocJSON sets the version selfdoc.json declares, keeping every
// other byte: the top-level "version", and the "version" of the last entry
// of the top-level "versions" array. A value the file does not declare is
// not added (a selfdoc.json declaring no version tracks none). changed is
// false when the file declares neither, or both already hold v.
func bumpSelfdocJSON(data []byte, v string) (out []byte, changed bool, err error) {
	spans, err := selfdocVersionSpans(data)
	if err != nil {
		return nil, false, err
	}
	literal, err := json.Marshal(v)
	if err != nil {
		return nil, false, err
	}
	out = data
	// Replace from the end, so the earlier spans keep their offsets.
	for i := len(spans) - 1; i >= 0; i-- {
		s := spans[i]
		if bytes.Equal(out[s.from:s.to], literal) {
			continue
		}
		next := make([]byte, 0, len(out)+len(literal))
		next = append(next, out[:s.from]...)
		next = append(next, literal...)
		out = append(next, out[s.to:]...)
		changed = true
	}
	return out, changed, nil
}

// selfdocVersionSpans are the spans of the version string literals
// bumpSelfdocJSON replaces, in document order. A version that is not a
// string is refused.
func selfdocVersionSpans(data []byte) ([]span, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	tok, err := dec.Token()
	if err != nil {
		return nil, fmt.Errorf("%s does not parse as JSON: %w", selfdocFile, err)
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, fmt.Errorf("%s is not a JSON object", selfdocFile)
	}
	var spans []span
	for dec.More() {
		key, err := objectKey(dec)
		if err != nil {
			return nil, err
		}
		switch key {
		case "version":
			s, err := stringValue(dec, data, "version")
			if err != nil {
				return nil, err
			}
			spans = append(spans, s)
		case "versions":
			s, found, err := lastEntryVersion(dec, data)
			if err != nil {
				return nil, err
			}
			if found {
				spans = append(spans, s)
			}
		default:
			if err := skipValue(dec); err != nil {
				return nil, err
			}
		}
	}
	if _, err := dec.Token(); err != nil {
		return nil, fmt.Errorf("%s does not parse as JSON: %w", selfdocFile, err)
	}
	sortSpans(spans)
	return spans, nil
}

func sortSpans(spans []span) {
	for i := 1; i < len(spans); i++ {
		for j := i; j > 0 && spans[j].from < spans[j-1].from; j-- {
			spans[j], spans[j-1] = spans[j-1], spans[j]
		}
	}
}

func objectKey(dec *json.Decoder) (string, error) {
	tok, err := dec.Token()
	if err != nil {
		return "", fmt.Errorf("%s does not parse as JSON: %w", selfdocFile, err)
	}
	key, ok := tok.(string)
	if !ok {
		return "", fmt.Errorf("%s does not parse as JSON: an object key is not a string", selfdocFile)
	}
	return key, nil
}

// stringValue reads the next value, which must be a string, and returns
// the span of its literal.
func stringValue(dec *json.Decoder, data []byte, name string) (span, error) {
	start := int(dec.InputOffset())
	tok, err := dec.Token()
	if err != nil {
		return span{}, fmt.Errorf("%s does not parse as JSON: %w", selfdocFile, err)
	}
	if _, ok := tok.(string); !ok {
		return span{}, fmt.Errorf("%s declares %s as %v, which is not a string", selfdocFile, strconv.Quote(name), tok)
	}
	end := int(dec.InputOffset())
	for start < end && data[start] != '"' {
		start++
	}
	return span{from: start, to: end}, nil
}

// lastEntryVersion reads the "versions" array and returns the span of its
// last entry's version, found false when the array is empty or its last
// entry declares none.
func lastEntryVersion(dec *json.Decoder, data []byte) (span, bool, error) {
	tok, err := dec.Token()
	if err != nil {
		return span{}, false, fmt.Errorf("%s does not parse as JSON: %w", selfdocFile, err)
	}
	if d, ok := tok.(json.Delim); !ok || d != '[' {
		return span{}, false, fmt.Errorf("%s declares \"versions\" as something other than an array", selfdocFile)
	}
	var last span
	var found bool
	for dec.More() {
		last, found = span{}, false
		tok, err := dec.Token()
		if err != nil {
			return span{}, false, fmt.Errorf("%s does not parse as JSON: %w", selfdocFile, err)
		}
		if d, ok := tok.(json.Delim); !ok || d != '{' {
			return span{}, false, fmt.Errorf("an entry of \"versions\" in %s is not an object", selfdocFile)
		}
		for dec.More() {
			key, err := objectKey(dec)
			if err != nil {
				return span{}, false, err
			}
			if key == "version" {
				if last, err = stringValue(dec, data, "versions[].version"); err != nil {
					return span{}, false, err
				}
				found = true
				continue
			}
			if err := skipValue(dec); err != nil {
				return span{}, false, err
			}
		}
		if _, err := dec.Token(); err != nil {
			return span{}, false, err
		}
	}
	if _, err := dec.Token(); err != nil {
		return span{}, false, err
	}
	return last, found, nil
}

// skipValue reads past one value of any shape.
func skipValue(dec *json.Decoder) error {
	depth := 0
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			return fmt.Errorf("%s ends inside a value", selfdocFile)
		}
		if err != nil {
			return fmt.Errorf("%s does not parse as JSON: %w", selfdocFile, err)
		}
		if d, ok := tok.(json.Delim); ok {
			switch d {
			case '{', '[':
				depth++
			default:
				depth--
			}
		}
		if depth == 0 {
			return nil
		}
	}
}
