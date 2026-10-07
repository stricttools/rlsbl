package testsupport

import (
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
)

// HTTPAnswer is one canned HTTP answer for a method and an exact URL.
type HTTPAnswer struct {
	Method string
	URL    string
	Status int
	Body   string
	Header map[string]string
}

// HTTPRequest is one request the fake transport received.
type HTTPRequest struct {
	Method string
	URL    string
	Header http.Header
	Body   string
}

// FakeHTTP is an http.RoundTripper answering from canned answers; no test
// opens a socket. A request no answer covers fails the test and returns an
// error to the caller.
type FakeHTTP struct {
	t        testing.TB
	mu       sync.Mutex
	answers  []HTTPAnswer
	requests []HTTPRequest
}

// NewFakeHTTP returns a transport answering from answers. Each answer must
// declare its method, URL, and status.
func NewFakeHTTP(t testing.TB, answers ...HTTPAnswer) *FakeHTTP {
	t.Helper()
	for _, a := range answers {
		if a.Method == "" || a.URL == "" || a.Status == 0 {
			t.Fatalf("testsupport: an HTTP answer must declare its method, URL, and status: %+v", a)
		}
	}
	return &FakeHTTP{t: t, answers: answers}
}

// Client is an HTTP client whose every request goes through the fake.
func (f *FakeHTTP) Client() *http.Client {
	return &http.Client{Transport: f}
}

// RoundTrip answers req from the canned answers.
func (f *FakeHTTP) RoundTrip(req *http.Request) (*http.Response, error) {
	var body string
	if req.Body != nil {
		data, err := io.ReadAll(req.Body)
		if err != nil {
			return nil, err
		}
		body = string(data)
	}
	url := req.URL.String()
	f.mu.Lock()
	f.requests = append(f.requests, HTTPRequest{Method: req.Method, URL: url, Header: req.Header.Clone(), Body: body})
	f.mu.Unlock()
	for _, a := range f.answers {
		if a.Method != req.Method || a.URL != url {
			continue
		}
		header := http.Header{}
		for k, v := range a.Header {
			header.Set(k, v)
		}
		return &http.Response{
			Status:        fmt.Sprintf("%d %s", a.Status, http.StatusText(a.Status)),
			StatusCode:    a.Status,
			Proto:         "HTTP/1.1",
			ProtoMajor:    1,
			ProtoMinor:    1,
			Header:        header,
			Body:          io.NopCloser(strings.NewReader(a.Body)),
			ContentLength: int64(len(a.Body)),
			Request:       req,
		}, nil
	}
	f.t.Errorf("fake HTTP: no answer for %s %s", req.Method, url)
	return nil, fmt.Errorf("fake HTTP: no answer for %s %s", req.Method, url)
}

// Requests is every request the fake received, in order.
func (f *FakeHTTP) Requests() []HTTPRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]HTTPRequest(nil), f.requests...)
}

// URLs is the URL of every request the fake received, in order.
func (f *FakeHTTP) URLs() []string {
	var urls []string
	for _, r := range f.Requests() {
		urls = append(urls, r.URL)
	}
	return urls
}
