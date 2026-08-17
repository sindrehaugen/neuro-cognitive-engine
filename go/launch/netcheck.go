package launch

import (
	"context"
	"crypto/tls"
	"fmt"
	"net"
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

// CheckPostgresTLS performs a TLS handshake to the Postgres TCP endpoint (managed/cloud probes).
// Caller should use context.WithTimeout(..., DefaultTLSProbeDeadline) or similar.
func CheckPostgresTLS(ctx context.Context, host, port string) error {
	addr := JoinHostPort(host, port)
	d := net.Dialer{}
	raw, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("tcp %s: %w", addr, err)
	}
	defer raw.Close()

	cfg := &tls.Config{
		MinVersion: tls.VersionTLS12,
		ServerName: host,
	}
	tlsConn := tls.Client(raw, cfg)

	deadline, ok := ctx.Deadline()
	if ok {
		_ = tlsConn.SetDeadline(deadline)
	} else {
		_ = tlsConn.SetDeadline(time.Now().Add(DefaultTLSProbeDeadline))
	}

	if err := tlsConn.HandshakeContext(ctx); err != nil {
		return fmt.Errorf("tls handshake %s: %w", addr, err)
	}
	_ = tlsConn.Close()
	return nil
}
