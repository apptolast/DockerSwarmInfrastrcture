package web

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
	"slices"
	"sync/atomic"
	"time"
)

// CertWatch remembers when the panel's certificate and the last accepted
// client certificate expire, for the banner.
type CertWatch struct {
	Server time.Time
	client atomic.Int64
}

// Client is the last accepted client certificate's NotAfter, or zero.
func (c *CertWatch) Client() time.Time {
	if u := c.client.Load(); u != 0 {
		return time.Unix(u, 0).UTC()
	}
	return time.Time{}
}

// Warnings lists certificates that expire within 30 days of now.
func (c *CertWatch) Warnings(now time.Time) []string {
	var out []string
	limit := now.Add(30 * 24 * time.Hour)
	if !c.Server.IsZero() && c.Server.Before(limit) {
		out = append(out, "El certificado del panel caduca el "+c.Server.Format("2006-01-02")+".")
	}
	if cl := c.Client(); !cl.IsZero() && cl.Before(limit) {
		out = append(out, "El certificado cliente de Traefik caduca el "+cl.Format("2006-01-02")+".")
	}
	return out
}

// ServerTLS is the mTLS listener's configuration: TLS 1.3 only, and only a
// client certificate issued by the private CA, with the expected common
// name and the clientAuth usage, gets in. Nothing else inside the cluster
// can use the panel, because NetworkPolicy cannot protect a NodePort.
func ServerTLS(certFile, keyFile, caFile, commonName string) (*tls.Config, *CertWatch, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, nil, fmt.Errorf("loading the server certificate: %w", err)
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		return nil, nil, fmt.Errorf("parsing the server certificate: %w", err)
	}
	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, nil, fmt.Errorf("reading the client CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, nil, errors.New("the client CA file has no certificate")
	}
	watch := &CertWatch{Server: leaf.NotAfter.UTC()}
	cfg := &tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{cert},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    pool,
		NextProtos:   []string{"h2", "http/1.1"},
		VerifyConnection: func(cs tls.ConnectionState) error {
			return checkClient(cs, commonName, watch)
		},
	}
	return cfg, watch, nil
}

func checkClient(cs tls.ConnectionState, commonName string, watch *CertWatch) error {
	if len(cs.PeerCertificates) == 0 || len(cs.VerifiedChains) == 0 {
		return errors.New("client certificate required")
	}
	leaf := cs.PeerCertificates[0]
	if leaf.Subject.CommonName != commonName {
		return errors.New("unexpected client certificate subject")
	}
	// Go accepts a leaf with no extended key usage as valid for any; the
	// panel requires clientAuth explicitly, so a server leaf never works.
	if !slices.Contains(leaf.ExtKeyUsage, x509.ExtKeyUsageClientAuth) {
		return errors.New("client certificate lacks clientAuth")
	}
	watch.client.Store(leaf.NotAfter.Unix())
	return nil
}
