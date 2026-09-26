package web

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log"
	"math/big"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type issued struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
}

func (i issued) tls() tls.Certificate {
	return tls.Certificate{Certificate: [][]byte{i.cert.Raw}, PrivateKey: i.key, Leaf: i.cert}
}

var serial int64

func issue(t *testing.T, cn string, parent *issued, isCA bool, eku []x509.ExtKeyUsage, dns []string, notAfter time.Time) issued {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial++
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: cn},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: notAfter,
		IsCA: isCA, BasicConstraintsValid: true, ExtKeyUsage: eku, DNSNames: dns,
		KeyUsage: x509.KeyUsageDigitalSignature,
	}
	if isCA {
		tmpl.KeyUsage |= x509.KeyUsageCertSign
	}
	signer, signerKey := tmpl, key
	if parent != nil {
		signer, signerKey = parent.cert, parent.key
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, signer, &key.PublicKey, signerKey)
	if err != nil {
		t.Fatal(err)
	}
	c, _ := x509.ParseCertificate(der)
	return issued{c, key}
}

func writePEM(t *testing.T, dir, name, kind string, der []byte) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, pem.EncodeToMemory(&pem.Block{Type: kind, Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestMutualTLS(t *testing.T) {
	year := time.Now().Add(365 * 24 * time.Hour)
	ca := issue(t, "ax-web CA", nil, true, nil, nil, year)
	other := issue(t, "other CA", nil, true, nil, nil, year)
	server := issue(t, "ax-web", &ca, false, []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, []string{"ax-web"}, year)
	client := issue(t, "edge-traefik", &ca, false, []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}, nil,
		time.Now().Add(10*24*time.Hour))
	wrongCA := issue(t, "edge-traefik", &other, false, []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}, nil, year)
	wrongCN := issue(t, "someone", &ca, false, []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}, nil, year)
	noEKU := issue(t, "edge-traefik", &ca, false, nil, nil, year)

	dir := t.TempDir()
	keyDER, _ := x509.MarshalECPrivateKey(server.key)
	cfg, watch, err := ServerTLS(
		writePEM(t, dir, "tls.crt", "CERTIFICATE", server.cert.Raw),
		writePEM(t, dir, "tls.key", "EC PRIVATE KEY", keyDER),
		writePEM(t, dir, "ca.crt", "CERTIFICATE", ca.cert.Raw),
		"edge-traefik")
	if err != nil {
		t.Fatal(err)
	}
	ln, err := tls.Listen("tcp", "127.0.0.1:0", cfg)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(w, "ok")
		}),
		ErrorLog: log.New(io.Discard, "", 0),
	}
	go func() { _ = srv.Serve(ln) }()
	defer srv.Close()

	roots := x509.NewCertPool()
	roots.AddCert(ca.cert)
	try := func(certs ...tls.Certificate) error {
		c := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{
			RootCAs: roots, ServerName: "ax-web", Certificates: certs, MinVersion: tls.VersionTLS13,
		}}}
		defer c.CloseIdleConnections()
		resp, err := c.Get("https://" + ln.Addr().String() + "/")
		if err != nil {
			return err
		}
		resp.Body.Close()
		return nil
	}
	if err := try(client.tls()); err != nil {
		t.Fatalf("the Traefik client was refused: %v", err)
	}
	refused := map[string][]tls.Certificate{
		"no certificate":     nil,
		"another CA":         {wrongCA.tls()},
		"another name":       {wrongCN.tls()},
		"server leaf":        {server.tls()},
		"no extended usages": {noEKU.tls()},
	}
	for name, certs := range refused {
		if err := try(certs...); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
	// TLS 1.2 is refused outright.
	conn, err := tls.Dial("tcp", ln.Addr().String(), &tls.Config{
		RootCAs: roots, ServerName: "ax-web", Certificates: []tls.Certificate{client.tls()},
		MaxVersion: tls.VersionTLS12,
	})
	if err == nil {
		conn.Close()
		t.Error("TLS 1.2 accepted")
	}
	if w := watch.Warnings(time.Now()); len(w) != 1 {
		t.Fatalf("the client certificate expires in 10 days: %v", w)
	}
}
