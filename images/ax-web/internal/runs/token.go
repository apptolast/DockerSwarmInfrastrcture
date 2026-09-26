package runs

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"unicode/utf8"
)

// maxTokenBytes bounds the credential file.
const maxTokenBytes = 8 << 10

var errToken = errors.New("credential unavailable")

// TokenReader reads the agent credential from a Kubernetes Secret volume
// once per run. The kubelet publishes each key as a symlink into a
// timestamped directory, so the path is resolved first, must stay inside
// the volume, and the resolved file is opened without following links and
// checked on the open descriptor. No error ever carries the value.
func TokenReader(dir, key string) func() (string, error) {
	return func() (string, error) {
		root, err := filepath.EvalSymlinks(dir)
		if err != nil {
			return "", errToken
		}
		path, err := filepath.EvalSymlinks(filepath.Join(dir, key))
		if err != nil || !strings.HasPrefix(path, root+string(filepath.Separator)) {
			return "", errToken
		}
		f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
		if err != nil {
			return "", errToken
		}
		defer f.Close()
		info, err := f.Stat()
		if err != nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maxTokenBytes {
			return "", errToken
		}
		data, err := io.ReadAll(io.LimitReader(f, maxTokenBytes+1))
		if err != nil || len(data) > maxTokenBytes {
			return "", errToken
		}
		value := strings.TrimRight(string(data), "\r\n")
		if value == "" || strings.ContainsAny(value, "\x00\r\n") || !utf8.ValidString(value) {
			return "", errToken
		}
		return value, nil
	}
}
