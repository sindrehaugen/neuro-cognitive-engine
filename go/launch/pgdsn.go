package launch

import (
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
)

const defaultPGPort = "5432"

func validatePort(p string) error {
	n, err := strconv.Atoi(p)
	if err != nil || n < 1 || n > 65535 {
		return fmt.Errorf("invalid port %q", p)
	}
	return nil
}

// PostgresAddrFromDSN extracts the primary host and port from postgresql://,
// postgres://, or libpq key=value DSN strings (including IPv6 and multi-host).
func PostgresAddrFromDSN(dsn string) (host, port string, err error) {
	dsn = strings.TrimSpace(dsn)
	if dsn == "" {
		return "", "", fmt.Errorf("empty PG_DSN")
	}
	// libpq key=value style (minimal): host=... port=...
	if strings.Contains(dsn, "=") && !strings.HasPrefix(dsn, "postgres://") && !strings.HasPrefix(dsn, "postgresql://") {
		return parseLibpqConn(dsn)
	}
	// Handle multi-host URIs (e.g. postgresql://user:pass@h1:5432,h2:5433/db)
	schemeIdx := strings.Index(dsn, "://")
	if schemeIdx > 0 {
		rest := dsn[schemeIdx+3:]
		pathEnd := strings.IndexAny(rest, "/?#")
		authority := rest
		suffix := ""
		if pathEnd >= 0 {
			authority = rest[:pathEnd]
			suffix = rest[pathEnd:]
		}
		hostsPart := authority
		userinfo := ""
		if atIdx := strings.LastIndex(authority, "@"); atIdx >= 0 {
			userinfo = authority[:atIdx+1]
			hostsPart = authority[atIdx+1:]
		}
		if commaIdx := strings.IndexByte(hostsPart, ','); commaIdx >= 0 {
			firstHost := strings.TrimSpace(hostsPart[:commaIdx])
			dsn = dsn[:schemeIdx+3] + userinfo + firstHost + suffix
		}
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
	if err := validatePort(p); err != nil {
		return "", "", fmt.Errorf("PG_DSN: %w", err)
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
		v = strings.Trim(strings.TrimSpace(v), "'\"")
		switch strings.TrimSpace(k) {
		case "host":
			if first, _, hasComma := strings.Cut(v, ","); hasComma {
				v = strings.TrimSpace(first)
			}
			host = strings.Trim(v, "[]")
		case "port":
			if first, _, hasComma := strings.Cut(v, ","); hasComma {
				v = strings.TrimSpace(first)
			}
			port = v
		}
	}
	if host == "" {
		return "", "", fmt.Errorf("libpq DSN missing host")
	}
	if err := validatePort(port); err != nil {
		return "", "", fmt.Errorf("libpq DSN: %w", err)
	}
	return host, port, nil
}

// JoinHostPort is a thin wrapper for net.JoinHostPort with IPv6 safety.
func JoinHostPort(host, port string) string {
	return net.JoinHostPort(host, port)
}
