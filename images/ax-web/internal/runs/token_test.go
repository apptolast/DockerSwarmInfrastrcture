package runs

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// kubeletVolume lays files out like a Secret volume: key -> ..data/key,
// ..data -> a timestamped directory.
func kubeletVolume(t *testing.T, key, value string) string {
	t.Helper()
	dir := t.TempDir()
	stamp := filepath.Join(dir, "..2026_09_26_00_00_00.1")
	if err := os.Mkdir(stamp, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stamp, key), []byte(value), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Base(stamp), filepath.Join(dir, "..data")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join("..data", key), filepath.Join(dir, key)); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestTokenReader(t *testing.T) {
	value := strings.Repeat("v", 30)
	dir := kubeletVolume(t, "k", value+"\n")
	got, err := TokenReader(dir, "k")()
	if err != nil || got != value {
		t.Fatalf("%v", err)
	}
	if _, err := TokenReader(dir, "missing")(); err == nil {
		t.Fatal("a missing key was read")
	}
}

func TestTokenReaderRefuses(t *testing.T) {
	outside := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.WriteFile(outside, []byte("x"), 0o400); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(dir, "escape")); err != nil {
		t.Fatal(err)
	}
	if _, err := TokenReader(dir, "escape")(); err == nil {
		t.Fatal("a link out of the volume was followed")
	}
	for name, content := range map[string]string{
		"empty": "", "multiline": "a\nb", "nul": "a\x00b", "huge": strings.Repeat("x", maxTokenBytes+1),
	} {
		d := kubeletVolume(t, "k", content)
		if _, err := TokenReader(d, "k")(); err == nil {
			t.Errorf("%s accepted", name)
		} else if strings.Contains(err.Error(), content) && content != "" {
			t.Errorf("%s: the error carries the value", name)
		}
	}
}
