package launch

import (
	"context"
	"fmt"
	"log/slog"
)

func runMultiuser(ctx context.Context, n UserNotifier, log *slog.Logger, appRoot string, env []string) error {
	dsn := LookupEnv(env, "PG_DSN")
	if dsn == "" {
		msg := "Database connection string (PG_DSN) is missing from configuration."
		n.Error("NCE", msg)
		return fmt.Errorf("PG_DSN missing")
	}
	host, port, err := PostgresAddrFromDSN(dsn)
	if err != nil {
		n.Error("NCE", "Could not read the Postgres address from PG_DSN. Contact IT.")
		if log != nil {
			log.Warn("pg_dsn_parse_failed", "err", err)
		}
		return err
	}

	for {
		tcpCtx, cancel := context.WithTimeout(ctx, pgTCPTimeout)
		tcpErr := CheckTCPConnectivity(tcpCtx, host, port)
		cancel()
		if tcpErr == nil {
			break
		}
		if log != nil {
			log.Warn("postgres_tcp_failed", "host", host, "port", port, "err", tcpErr)
		}
		addr := fmt.Sprintf("%s:%s", host, port)
		msg := fmt.Sprintf(
			"NCE cannot open a TCP connection to the database at %s.\n\n"+
				"Is your VPN connected? Click Yes to try again, or No to exit.",
			addr,
		)
		if n.ConfirmConnectivity("NCE — connection check", msg) {
			continue
		}
		n.Error("NCE", "Could not reach the database. Verify VPN and PG_DSN, then launch NCE again.")
		return fmt.Errorf("postgres tcp: %w", tcpErr)
	}

	// Azure AD UPN refresh when a cache hook exists is deferred to a later phase; TCP proves reachability first.
	if log != nil {
		log.Info("multiuser_tcp_ok")
	}

	return runMCPServer(ctx, n, appRoot, env, log)
}
