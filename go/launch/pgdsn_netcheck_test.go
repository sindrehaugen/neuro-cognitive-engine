package launch

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"io"
	"math/big"
	"net"
	"strings"
	"testing"
	"time"
)

func TestPostgresAddrFromDSN(t *testing.T) {
	cases := []struct {
		name     string
		dsn      string
		wantHost string
		wantPort string
		wantErr  bool
	}{
		{
			name:     "standard_uri",
			dsn:      "postgresql://user:pass@db.example.com:6432/nce?sslmode=require",
			wantHost: "db.example.com",
			wantPort: "6432",
		},
		{
			name:     "default_port",
			dsn:      "postgres://user@db.example.com/nce",
			wantHost: "db.example.com",
			wantPort: "5432",
		},
		{
			name:     "ipv6_uri",
			dsn:      "postgresql://user@[2001:db8::1]:6432/nce",
			wantHost: "2001:db8::1",
			wantPort: "6432",
		},
		{
			name:     "multi_host_uri",
			dsn:      "postgresql://user:pass@primary.db:5432,replica.db:5433/nce?sslmode=require",
			wantHost: "primary.db",
			wantPort: "5432",
		},
		{
			name:     "libpq_kv_multi_host",
			dsn:      "host=h1.internal,h2.internal port=5434,5435 dbname=nce sslmode=verify-full",
			wantHost: "h1.internal",
			wantPort: "5434",
		},
		{
			name:    "invalid_port",
			dsn:     "postgresql://user@db.example.com:99999/nce",
			wantErr: true,
		},
		{
			name:    "empty",
			dsn:     "   ",
			wantErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h, p, err := PostgresAddrFromDSN(tc.dsn)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got host=%q port=%q", h, p)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if h != tc.wantHost || p != tc.wantPort {
				t.Fatalf("got (%q, %q), want (%q, %q)", h, p, tc.wantHost, tc.wantPort)
			}
		})
	}
}

func makeSelfSignedCert(t *testing.T) (tls.Certificate, *x509.CertPool) {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	tmpl := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "127.0.0.1"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageKeyEncipherment | x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &priv.PublicKey, priv)
	if err != nil {
		t.Fatalf("CreateCertificate: %v", err)
	}
	certObj, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("ParseCertificate: %v", err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(certObj)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: priv}, pool
}

func TestCheckPostgresTLS_SSLRequestAcceptAndReject(t *testing.T) {
	cert, pool := makeSelfSignedCert(t)

	t.Run("accept_S_and_upgrade_tls", func(t *testing.T) {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatalf("listen: %v", err)
		}
		defer ln.Close()

		done := make(chan error, 1)
		go func() {
			conn, err := ln.Accept()
			if err != nil {
				done <- err
				return
			}
			defer conn.Close()
			buf := make([]byte, 8)
			if _, err := io.ReadFull(conn, buf); err != nil {
				done <- err
				return
			}
			if !bytes.Equal(buf, pgSSLRequest) {
				done <- io.ErrUnexpectedEOF
				return
			}
			if _, err := conn.Write([]byte{'S'}); err != nil {
				done <- err
				return
			}
			tlsConn := tls.Server(conn, &tls.Config{
				Certificates: []tls.Certificate{cert},
				MinVersion:   tls.VersionTLS12,
			})
			done <- tlsConn.Handshake()
		}()

		host, port, _ := net.SplitHostPort(ln.Addr().String())
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		err = checkPostgresTLSWithConfig(ctx, host, port, &tls.Config{RootCAs: pool})
		if err != nil {
			t.Fatalf("checkPostgresTLSWithConfig failed: %v", err)
		}
		if srvErr := <-done; srvErr != nil {
			t.Fatalf("server handshake failed: %v", srvErr)
		}
	})

	t.Run("reject_N_fails_closed", func(t *testing.T) {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatalf("listen: %v", err)
		}
		defer ln.Close()

		go func() {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			defer conn.Close()
			buf := make([]byte, 8)
			_, _ = io.ReadFull(conn, buf)
			_, _ = conn.Write([]byte{'N'})
		}()

		host, port, _ := net.SplitHostPort(ln.Addr().String())
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		err = checkPostgresTLSWithConfig(ctx, host, port, &tls.Config{RootCAs: pool})
		if err == nil || !strings.Contains(err.Error(), "rejected TLS") {
			t.Fatalf("expected TLS rejection error, got: %v", err)
		}
	})
}
