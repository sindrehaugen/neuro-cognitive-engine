package launch

import (
	"fmt"
	"net"
	"net/url"
	"strings"
)

const defaultPGPort = "5432"

// PostgresAddrFromDSN extracts host and port from postgresql:// or postgres:// URLs.
func PostgresAddrFromDSN(dsn string) (host, port string, err error) {
	dsn = strings.TrimSpace(dsn)
	if dsn == "" {
		return "", "", fmt.Errorf("empty PG_DSN")
	}
	// libpq key=value style (minimal): host=... port=...
	if strings.Contains(dsn, "=") && !strings.HasPrefix(dsn, "postgres://") && !strings.HasPrefix(dsn, "postgresql://") {
		return parseLibpqConn(dsn)
	}
	u, err := url.Parse(dsn)
	if err != nil {
		return "", "", fmt.Errorf("parse PG_DSN: %w", err)
	}
	h := u.Hostname()
	if h == "" {
		return "", "", fmt.Errorf("PG_DSN missing host")
	}
	p := u.Port()
	if p == "" {
		p = defaultPGPort
	}
	return h, p, nil
}

func parseLibpqConn(dsn string) (host, port string, err error) {
	host = "localhost"
	port = defaultPGPort
	for _, part := range strings.Fields(dsn) {
		part = strings.TrimSpace(part)
		k, v, ok := strings.Cut(part, "=")
		if !ok {
			continue
		}
		switch strings.TrimSpace(k) {
		case "host":
			host = strings.TrimSpace(v)
		case "port":
			port = strings.TrimSpace(v)
		}
	}
	if host == "" {
		return "", "", fmt.Errorf("libpq DSN missing host")
	}
	return host, port, nil
}

// JoinHostPort is a thin wrapper for net.JoinHostPort with IPv6 safety.
func JoinHostPort(host, port string) string {
	return net.JoinHostPort(host, port)
}
