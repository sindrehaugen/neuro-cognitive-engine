package launch

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/nce/tri-stack/auth"
)

var (
	refreshCloudAccessToken = auth.RefreshCloudAccessToken
	checkPostgresTLS        = CheckPostgresTLS
	runCloudMCPServer       = runMCPServer
)

func runCloud(ctx context.Context, n UserNotifier, log *slog.Logger, appRoot string, env []string) error {
	authCtx, cancel := context.WithTimeout(ctx, auth.RecommendedCloudOAuthTimeout)
	defer cancel()
	tok, err := refreshCloudAccessToken(authCtx, log, "")
	if err != nil {
		switch {
		case errors.Is(err, auth.ErrCloudOAuthNotConfigured):
			n.Error("NCE", "Cloud sign-in is not configured. Re-run the NCE installer for Cloud mode.")
		default:
			n.Error("NCE", "Could not refresh your cloud session. Check the network or complete sign-in, then try again.")
		}
		if log != nil {
			log.Warn("msal_refresh_failed", "err", err)
		}
		return fmt.Errorf("msal: %w", err)
	}
	if strings.TrimSpace(tok.Token) != "" {
		env = UpsertEnv(env, "NCE_CLOUD_ACCESS_TOKEN", tok.Token)
	}

	dsn := LookupEnv(env, "PG_DSN")
	if dsn == "" {
		n.Error("NCE", "Database connection string (PG_DSN) is missing from configuration.")
		return fmt.Errorf("PG_DSN missing")
	}
	host, port, err := PostgresAddrFromDSN(dsn)
	if err != nil {
		n.Error("NCE", "Could not read the managed database address from PG_DSN.")
		if log != nil {
			log.Warn("pg_dsn_parse_failed", "err", err)
		}
		return err
	}

	tlsCtx, tcancel := context.WithTimeout(ctx, pgTLSTimeout)
	defer tcancel()
	if err := checkPostgresTLS(tlsCtx, host, port); err != nil {
		n.Error("NCE", "Cannot reach the secure database endpoint. Check VPN or firewall settings.")
		if log != nil {
			log.Warn("cloud_tls_failed", "host", host, "port", port, "err", err)
		}
		return fmt.Errorf("postgres tls: %w", err)
	}

	return runCloudMCPServer(ctx, n, appRoot, env, log)
}
