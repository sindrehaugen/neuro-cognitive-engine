package auth

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/AzureAD/microsoft-authentication-library-for-go/apps/public"
	"github.com/pkg/browser"
)

const defaultGraphScope = "https://graph.microsoft.com/User.Read"

// RecommendedCloudOAuthTimeout bounds MSAL AcquireTokenSilent network I/O to Microsoft Entra ID.
// Hardware subprocess probes use a stricter per-command budget (<5s); OAuth refresh is separate (§6.4 vs AV ops).
const RecommendedCloudOAuthTimeout = 45 * time.Second

// CloudToken holds a bearer access token. Callers must not log Token contents.
type CloudToken struct {
	Token     string
	ExpiresOn time.Time
}

// EnsureCloudAccessToken is the cross-platform Cloud OAuth helper (§6.4): reads bridges.json for
// tenant_id and client_id only (never bridge OAuth secrets in this flow), loads the MSAL opaque cache
// from disk (path from bridges or default), and falls back to the OS credential store when the file is
// empty and MirrorMSALToKeychain was used. MSAL refreshes cached tokens via AcquireTokenSilent when possible;
// otherwise this helper runs the device code flow (opens the verification URL in the system browser when available),
// then persists the updated opaque cache via dualMSALCache.
//
// Callers must not log access tokens, refresh tokens, cache payloads, or raw bridges.json. Wrap ctx with
// RecommendedCloudOAuthTimeout or similar; that deadline is separate from the <5s hardware probe budget.
//
// bridgesPath may be empty to use DefaultBridgesPath().
func EnsureCloudAccessToken(ctx context.Context, log *slog.Logger, bridgesPath string) (CloudToken, error) {
	if log == nil {
		log = slog.Default()
	}
	path := bridgesPath
	if path == "" {
		var err error
		path, err = DefaultBridgesPath()
		if err != nil {
			return CloudToken{}, err
		}
	}
	bf, err := ReadBridges(path)
	if err != nil {
		log.Debug("read_bridges_failed")
		return CloudToken{}, fmt.Errorf("read bridges: %w", err)
	}
	if bf == nil || bf.Cloud == nil {
		return CloudToken{}, ErrCloudOAuthNotConfigured
	}
	cloud := bf.Cloud
	if cloud.TenantID == "" || cloud.ClientID == "" {
		return CloudToken{}, fmt.Errorf("%w missing tenant_id/client_id", ErrCloudOAuthNotConfigured)
	}

	cachePath, err := ResolveMSALCachePath(cloud)
	if err != nil {
		return CloudToken{}, err
	}
	dual := newDualMSALCache(cachePath, cloud.MirrorMSALToKeychain)

	authority := fmt.Sprintf("https://login.microsoftonline.com/%s", cloud.TenantID)
	app, err := public.New(cloud.ClientID,
		public.WithAuthority(authority),
		public.WithCache(dual),
	)
	if err != nil {
		log.Warn("msal_public_client_init_failed")
		return CloudToken{}, err
	}

	scopes := cloud.Scopes
	if len(scopes) == 0 {
		scopes = []string{defaultGraphScope}
	}

	accounts, err := app.Accounts(ctx)
	if err != nil {
		log.Warn("msal_accounts_failed")
		return CloudToken{}, err
	}

	if len(accounts) > 0 {
		ar, silentErr := app.AcquireTokenSilent(ctx, scopes, public.WithSilentAccount(accounts[0]), public.WithTenantID(cloud.TenantID))
		if silentErr == nil {
			log.Info("msal_silent_ok")
			return CloudToken{Token: ar.AccessToken, ExpiresOn: ar.ExpiresOn}, nil
		}
		log.Warn("msal_acquire_token_silent_failed")
	}

	log.Info("msal_device_code_start")
	dc, err := app.AcquireTokenByDeviceCode(ctx, scopes, public.WithTenantID(cloud.TenantID))
	if err != nil {
		log.Warn("msal_device_code_request_failed")
		return CloudToken{}, err
	}
	if u := dc.Result.VerificationURL; u != "" {
		if openErr := browser.OpenURL(u); openErr != nil && log != nil {
			log.Debug("device_code_open_browser_skipped")
		}
	}
	if log != nil && dc.Result.Message != "" {
		log.Info("msal_device_code_instructions", "message", dc.Result.Message)
	}

	ar, err := dc.AuthenticationResult(ctx)
	if err != nil {
		log.Warn("msal_device_code_failed")
		return CloudToken{}, err
	}
	log.Info("msal_device_code_ok")
	return CloudToken{Token: ar.AccessToken, ExpiresOn: ar.ExpiresOn}, nil
}

// RefreshCloudAccessToken aliases EnsureCloudAccessToken for Cloud mode startup naming (§6.4).
func RefreshCloudAccessToken(ctx context.Context, log *slog.Logger, bridgesPath string) (CloudToken, error) {
	return EnsureCloudAccessToken(ctx, log, bridgesPath)
}
