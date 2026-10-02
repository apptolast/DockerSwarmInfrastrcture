package web

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"errors"
	"io/fs"
	"net/http"
	"path"
	"sort"
)

//go:embed static
var static embed.FS

// Asset tokens in static/index.html, replaced with the hashed paths.
const (
	tokenCSS = "{{APP_CSS}}"
	tokenJS  = "{{APP_JS}}"
	// AssetCache is for content-addressed assets: their name changes
	// with their content.
	AssetCache = "public, max-age=31536000, immutable"
)

// StaticFS is the embedded UI (index.html, css/, js/, favicon.svg).
func StaticFS() fs.FS {
	sub, err := fs.Sub(static, "static")
	if err != nil {
		panic(err)
	}
	return sub
}

// bundle is the UI as served: index.html pointing at one concatenated
// stylesheet and one script, each named by its sha256.
type bundle struct {
	index   []byte
	css     []byte
	js      []byte
	cssName string
	jsName  string
	favicon []byte
	err     error
}

// concat joins the files matching pattern in lexical order, "\n"
// between them.
func concat(fsys fs.FS, pattern string) ([]byte, error) {
	names, err := fs.Glob(fsys, pattern)
	if err != nil {
		return nil, err
	}
	sort.Strings(names)
	var out [][]byte
	for _, n := range names {
		data, err := fs.ReadFile(fsys, n)
		if err != nil {
			return nil, err
		}
		out = append(out, data)
	}
	return bytes.Join(out, []byte("\n")), nil
}

func hashName(base, ext string, data []byte) string {
	sum := sha256.Sum256(data)
	return base + "." + hex.EncodeToString(sum[:])[:12] + "." + ext
}

// buildBundle reads the UI from fsys once, at start-up.
func buildBundle(fsys fs.FS) *bundle {
	b := &bundle{}
	index, err := fs.ReadFile(fsys, "index.html")
	if err != nil {
		b.err = errors.New("falta index.html")
		return b
	}
	if b.css, err = concat(fsys, "css/*.css"); err != nil {
		b.err = err
		return b
	}
	if b.js, err = concat(fsys, "js/*.js"); err != nil {
		b.err = err
		return b
	}
	b.cssName, b.jsName = hashName("app", "css", b.css), hashName("app", "js", b.js)
	index = bytes.ReplaceAll(index, []byte(tokenCSS), []byte("/assets/"+b.cssName))
	b.index = bytes.ReplaceAll(index, []byte(tokenJS), []byte("/assets/"+b.jsName))
	b.favicon, _ = fs.ReadFile(fsys, "favicon.svg")
	return b
}

func (s *Server) index(w http.ResponseWriter, _ *http.Request) {
	if s.assets.err != nil {
		writeError(w, http.StatusInternalServerError, "la interfaz no está disponible")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(s.assets.index)
}

func (s *Server) asset(w http.ResponseWriter, r *http.Request) {
	name := path.Base(r.PathValue("name"))
	var data []byte
	var ctype string
	switch {
	case s.assets.err != nil:
	case name == s.assets.cssName:
		data, ctype = s.assets.css, "text/css; charset=utf-8"
	case name == s.assets.jsName:
		data, ctype = s.assets.js, "text/javascript; charset=utf-8"
	}
	if ctype == "" {
		writeError(w, http.StatusNotFound, "no existe")
		return
	}
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Cache-Control", AssetCache)
	_, _ = w.Write(data)
}

func (s *Server) favicon(w http.ResponseWriter, _ *http.Request) {
	if len(s.assets.favicon) == 0 {
		writeError(w, http.StatusNotFound, "no existe")
		return
	}
	w.Header().Set("Content-Type", "image/svg+xml")
	w.Header().Set("Cache-Control", "public, max-age=86400")
	_, _ = w.Write(s.assets.favicon)
}
