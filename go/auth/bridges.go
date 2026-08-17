package auth

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// ErrCloudOAuthNotConfigured is returned when bridges has no cloud/Azure section.
var ErrCloudOAuthNotConfigured = errors.New("cloud oauth not configured")

// ErrNoMSALAccount is retained for API compatibility; EnsureCloudAccessToken runs device code when the cache is empty.
var ErrNoMSALAccount = errors.New("no msal account in cache; interactive sign-in required")

// BridgesFile matches §6.3 layout (subset). OAuth token material must never be logged by callers.
type BridgesFile struct {
	Cloud *CloudAzure `json:"cloud,omitempty"`
}

// CloudAzure holds Entra ID app registration metadata — not tokens.
type CloudAzure struct {
	TenantID     string   `json:"tenant_id"`
	ClientID     string   `json:"client_id"`
	Scopes       []string `json:"scopes"`
	// MsalCacheFile relative to NCE data dir unless absolute (e.g. msal_cache.bin).
	MsalCacheFile string `json:"msal_cache_file,omitempty"`
	// MirrorMSALToKeychain duplicates the opaque MSAL blob to the OS credential store when true.
	MirrorMSALToKeychain bool `json:"mirror_msal_to_keychain,omitempty"`
}

// NCEDataDir resolves %APPDATA%/NCE (Windows), ~/Library/Application Support/NCE (macOS),
// or XDG_CONFIG_HOME/NCE.
func NCEDataDir() (string, error) {
	base, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(base, "NCE"), nil
}

// DefaultBridgesPath is the documented location from §6.3.
func DefaultBridgesPath() (string, error) {
	dir, err := NCEDataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "bridges.json"), nil
}

// ReadBridges loads bridges.json. It never prints file contents.
func ReadBridges(path string) (*BridgesFile, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var f BridgesFile
	if err := json.Unmarshal(b, &f); err != nil {
		return nil, fmt.Errorf("bridges.json: %w", err)
	}
	return &f, nil
}

// ResolveMSALCachePath returns absolute path for the serialized MSAL cache.
func ResolveMSALCachePath(cloud *CloudAzure) (string, error) {
	if cloud == nil {
		return "", fmt.Errorf("cloud config nil")
	}
	name := cloud.MsalCacheFile
	if name == "" {
		name = "msal_cache.bin"
	}
	if filepath.IsAbs(name) {
		return name, nil
	}
	dir, err := NCEDataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, name), nil
}
