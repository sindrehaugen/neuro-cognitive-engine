package launch

import (
	"context"
	"log/slog"
	"testing"

	"github.com/nce/tri-stack/auth"
)

func TestRunCloud_InjectsCloudAccessToken(t *testing.T) {
	origRefresh := refreshCloudAccessToken
	origCheckTLS := checkPostgresTLS
	origRunMCP := runCloudMCPServer
	t.Cleanup(func() {
		refreshCloudAccessToken = origRefresh
		checkPostgresTLS = origCheckTLS
		runCloudMCPServer = origRunMCP
	})

	refreshCloudAccessToken = func(ctx context.Context, log *slog.Logger, bridgesPath string) (auth.CloudToken, error) {
		return auth.CloudToken{Token: "cloud-access-token-123"}, nil
	}
	checkPostgresTLS = func(ctx context.Context, host, port string) error {
		return nil
	}

	var capturedEnv []string
	runCloudMCPServer = func(ctx context.Context, n UserNotifier, appRoot string, env []string, log *slog.Logger) error {
		capturedEnv = append([]string(nil), env...)
		return nil
	}

	baseEnv := []string{
		"PG_DSN=postgresql://user:pass@db.example.com:5432/nce",
		"NCE_CLOUD_ACCESS_TOKEN=stale-token",
	}
	if err := runCloud(context.Background(), LogNotifier{}, nil, "/app", baseEnv); err != nil {
		t.Fatalf("runCloud failed: %v", err)
	}

	got := LookupEnv(capturedEnv, "NCE_CLOUD_ACCESS_TOKEN")
	if got != "cloud-access-token-123" {
		t.Fatalf("NCE_CLOUD_ACCESS_TOKEN = %q, want %q", got, "cloud-access-token-123")
	}
}

func TestRunCloud_BlankTokenDoesNotInjectEmptyEnv(t *testing.T) {
	origRefresh := refreshCloudAccessToken
	origCheckTLS := checkPostgresTLS
	origRunMCP := runCloudMCPServer
	t.Cleanup(func() {
		refreshCloudAccessToken = origRefresh
		checkPostgresTLS = origCheckTLS
		runCloudMCPServer = origRunMCP
	})

	refreshCloudAccessToken = func(ctx context.Context, log *slog.Logger, bridgesPath string) (auth.CloudToken, error) {
		return auth.CloudToken{Token: "   "}, nil
	}
	checkPostgresTLS = func(ctx context.Context, host, port string) error {
		return nil
	}

	var capturedEnv []string
	runCloudMCPServer = func(ctx context.Context, n UserNotifier, appRoot string, env []string, log *slog.Logger) error {
		capturedEnv = append([]string(nil), env...)
		return nil
	}

	baseEnv := []string{"PG_DSN=postgresql://user:pass@db.example.com:5432/nce"}
	if err := runCloud(context.Background(), LogNotifier{}, nil, "/app", baseEnv); err != nil {
		t.Fatalf("runCloud failed: %v", err)
	}

	if got := LookupEnv(capturedEnv, "NCE_CLOUD_ACCESS_TOKEN"); got != "" {
		t.Fatalf("expected empty NCE_CLOUD_ACCESS_TOKEN when token is blank, got %q", got)
	}
}
