package launch

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"strings"
	"time"
)

const (
	// DefaultTCPProbeDeadline is the maximum time for a plain TCP reachability check.
	DefaultTCPProbeDeadline = 6 * time.Second
	// DefaultTLSProbeDeadline includes TCP connect + TLS handshake.
	DefaultTLSProbeDeadline = 10 * time.Second
)

// CheckTCPConnectivity dials host:port with a deadline derived from ctx (caller should use context.WithTimeout).
func CheckTCPConnectivity(ctx context.Context, host, port string) error {
	addr := JoinHostPort(host, port)
	d := net.Dialer{}
	c, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("tcp %s: %w", addr, err)
	}
	_ = c.Close()
	return nil
}

// pgSSLRequest is the 8-byte PostgreSQL wire-protocol SSL negotiation packet
// (length=8, protocol=80877103).
var pgSSLRequest = []byte{0x00, 0x00, 0x00, 0x08, 0x04, 0xd2, 0x16, 0x2f}

// CheckPostgresTLS performs a PostgreSQL SSLRequest + TLS handshake to verify
// an encrypted database endpoint (managed/cloud probes).
// Caller should use context.WithTimeout(..., DefaultTLSProbeDeadline) or similar.
func CheckPostgresTLS(ctx context.Context, host, port string) error {
	return checkPostgresTLSWithConfig(ctx, host, port, nil)
}

func checkPostgresTLSWithConfig(ctx context.Context, host, port string, baseCfg *tls.Config) error {
	cleanHost := strings.Trim(host, "[]")
	addr := JoinHostPort(cleanHost, port)
	d := net.Dialer{Timeout: DefaultTLSProbeDeadline}
	raw, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("tcp %s: %w", addr, err)
	}
	defer raw.Close()

	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(DefaultTLSProbeDeadline)
	}
	_ = raw.SetDeadline(deadline)

	if _, err := raw.Write(pgSSLRequest); err != nil {
		return fmt.Errorf("postgres SSLRequest write %s: %w", addr, err)
	}
	var reply [1]byte
	if _, err := io.ReadFull(raw, reply[:]); err != nil {
		return fmt.Errorf("postgres SSLRequest reply %s: %w", addr, err)
	}
	switch reply[0] {
	case 'S':
		// Server agrees to upgrade connection to TLS.
	case 'N':
		return fmt.Errorf("postgres server %s rejected TLS (SSLRequest returned 'N')", addr)
	default:
		return fmt.Errorf("postgres server %s unexpected SSLRequest reply %q", addr, reply[0])
	}

	var cfg *tls.Config
	if baseCfg != nil {
		cfg = baseCfg.Clone()
	} else {
		cfg = &tls.Config{}
	}
	if cfg.MinVersion == 0 || cfg.MinVersion < tls.VersionTLS12 {
		cfg.MinVersion = tls.VersionTLS12
	}
	if cfg.ServerName == "" {
		cfg.ServerName = cleanHost
	}

	tlsConn := tls.Client(raw, cfg)
	_ = tlsConn.SetDeadline(deadline)
	if err := tlsConn.HandshakeContext(ctx); err != nil {
		return fmt.Errorf("tls handshake %s: %w", addr, err)
	}
	_ = tlsConn.Close()
	return nil
}
