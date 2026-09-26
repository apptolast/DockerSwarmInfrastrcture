package runs

import (
	"testing"
)

func TestBufferOffsetsAndReplay(t *testing.T) {
	b := NewBuffer(1 << 10)
	b.Append(StreamStdout, []byte("hola "))
	b.Append(StreamStderr, []byte("error\n"))
	chunks, trunc, closed, _ := b.Read(0)
	if trunc || closed || len(chunks) != 2 || chunks[0].End != 5 || chunks[1].End != 11 ||
		chunks[1].Stream != StreamStderr {
		t.Fatalf("%+v", chunks)
	}
	// Resume after the first chunk, and from the middle of one.
	if c, _, _, _ := b.Read(5); len(c) != 1 || c[0].Text != "error\n" {
		t.Fatalf("%+v", c)
	}
	if c, _, _, _ := b.Read(7); len(c) != 1 || c[0].Text != "ror\n" {
		t.Fatalf("%+v", c)
	}
	if c, _, _, _ := b.Read(11); len(c) != 0 {
		t.Fatalf("%+v", c)
	}
}

func TestBufferDropsOldest(t *testing.T) {
	b := NewBuffer(10)
	b.Append(StreamStdout, []byte("12345"))
	b.Append(StreamStdout, []byte("67890"))
	b.Append(StreamStdout, []byte("abcde"))
	c, trunc, _, _ := b.Read(0)
	if !trunc || len(c) != 2 || c[0].Text != "67890" {
		t.Fatalf("%v %+v", trunc, c)
	}
	if _, trunc, _, _ := b.Read(5); trunc {
		t.Fatal("offset 5 is still retained")
	}
}

func TestBufferUTF8Split(t *testing.T) {
	b := NewBuffer(1 << 10)
	e := []byte("ñ") // two bytes
	b.Append(StreamStdout, []byte{'a', e[0]})
	c, _, _, _ := b.Read(0)
	if len(c) != 1 || c[0].Text != "a" {
		t.Fatalf("%+v", c)
	}
	b.Append(StreamStdout, []byte{e[1], 'b'})
	c, _, _, _ = b.Read(0)
	if len(c) != 2 || c[1].Text != "ñb" {
		t.Fatalf("%+v", c)
	}
	// An incomplete tail is flushed at close.
	b.Append(StreamStderr, []byte{e[0]})
	b.Close()
	c, _, closed, _ := b.Read(0)
	if !closed || len(c) != 3 || c[2].Stream != StreamStderr {
		t.Fatalf("%+v", c)
	}
}

func TestBufferNotifies(t *testing.T) {
	b := NewBuffer(1 << 10)
	_, _, _, wait := b.Read(0)
	b.Append(StreamSystem, []byte("x"))
	select {
	case <-wait:
	default:
		t.Fatal("no notification")
	}
	_, _, _, wait = b.Read(1)
	b.Close()
	select {
	case <-wait:
	default:
		t.Fatal("no notification on close")
	}
}
