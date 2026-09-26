package runs

import (
	"sync"
	"unicode/utf8"
)

// Stream names in the output log.
const (
	StreamStdout = "out"
	StreamStderr = "err"
	StreamSystem = "sys"
)

// Chunk is one piece of the run's log. End is the log offset just past it,
// which the browser sends back as Last-Event-ID to resume.
type Chunk struct {
	Stream string
	Text   string
	End    int64
}

// Buffer is a bounded, append-only log addressed by byte offsets. The
// oldest chunks are dropped once it holds more than max bytes, so a reader
// that falls behind is told it lost part of the output.
type Buffer struct {
	mu      sync.Mutex
	max     int
	chunks  []Chunk
	size    int
	start   int64 // offset of the first retained byte
	end     int64 // offset past the last byte
	closed  bool
	notify  chan struct{}
	partial map[string][]byte // incomplete UTF-8 tail per stream
}

// NewBuffer holds at most max bytes of text.
func NewBuffer(max int) *Buffer {
	return &Buffer{max: max, notify: make(chan struct{}), partial: map[string][]byte{}}
}

// Append adds data from stream. A multi-byte character split across two
// guest chunks is held back until it is complete.
func (b *Buffer) Append(stream string, data []byte) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return
	}
	data = append(b.partial[stream], data...)
	cut := completeUTF8(data)
	if cut < len(data) {
		b.partial[stream] = append([]byte(nil), data[cut:]...)
	} else {
		delete(b.partial, stream)
	}
	b.appendLocked(stream, data[:cut])
}

func (b *Buffer) appendLocked(stream string, data []byte) {
	if len(data) == 0 {
		return
	}
	b.end += int64(len(data))
	b.chunks = append(b.chunks, Chunk{Stream: stream, Text: string(data), End: b.end})
	b.size += len(data)
	for b.size > b.max && len(b.chunks) > 1 {
		b.size -= len(b.chunks[0].Text)
		b.start = b.chunks[0].End
		b.chunks = b.chunks[1:]
	}
	close(b.notify)
	b.notify = make(chan struct{})
}

// Close flushes held-back bytes and ends the log.
func (b *Buffer) Close() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return
	}
	for _, s := range []string{StreamStdout, StreamStderr} {
		if p := b.partial[s]; len(p) > 0 {
			b.appendLocked(s, p)
		}
	}
	b.partial = map[string][]byte{}
	b.closed = true
	close(b.notify)
}

// Read returns the chunks after offset from, whether output before them was
// dropped, whether the log is closed, and a channel closed on the next
// change.
func (b *Buffer) Read(from int64) (out []Chunk, truncated, closed bool, wait <-chan struct{}) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if from < b.start {
		truncated = from < b.start && b.start > 0
		from = b.start
	}
	for _, c := range b.chunks {
		if c.End > from {
			if start := c.End - int64(len(c.Text)); start < from {
				// Resume mid-chunk: cut at a character boundary.
				text := c.Text[from-start:]
				for len(text) > 0 && !utf8.RuneStart(text[0]) {
					text = text[1:]
				}
				c.Text = text
			}
			out = append(out, c)
		}
	}
	return out, truncated, b.closed, b.notify
}

// End is the offset past the last byte.
func (b *Buffer) End() int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.end
}

// completeUTF8 is the length of the longest prefix of p that does not end
// in an incomplete character. Invalid bytes are left for the JSON encoder,
// which replaces them.
func completeUTF8(p []byte) int {
	for i := len(p) - 1; i >= 0 && i >= len(p)-utf8.UTFMax; i-- {
		if utf8.RuneStart(p[i]) {
			if !utf8.FullRune(p[i:]) {
				return i
			}
			return len(p)
		}
	}
	return len(p)
}
