package paths

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// DataDir is the per-user NCE config root (mode.txt, .env).
// Windows: %APPDATA%\NCE
// macOS: ~/Library/Application Support/NCE
// Other: ~/.config/nce
func DataDir() (string, error) {
	switch runtime.GOOS {
	case "windows":
		appData := os.Getenv("APPDATA")
		if appData == "" {
			return "", fmt.Errorf("APPDATA is not set")
		}
		return filepath.Join(appData, "NCE"), nil
	case "darwin":
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, "Library", "Application Support", "NCE"), nil
	default:
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, ".config", "nce"), nil
	}
}

// LogDir is the IT diagnostics log root (Deployment Plan §6.4).
// Windows: %APPDATA%\NCE\logs
// macOS: ~/Library/Logs/NCE (per Phase 4 spec; distinct from DataDir)
// Other: <DataDir>/logs
func LogDir() (string, error) {
	switch runtime.GOOS {
	case "windows":
		appData := os.Getenv("APPDATA")
		if appData == "" {
			return "", fmt.Errorf("APPDATA is not set")
		}
		return filepath.Join(appData, "NCE", "logs"), nil
	case "darwin":
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, "Library", "Logs", "NCE"), nil
	default:
		d, err := DataDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(d, "logs"), nil
	}
}

// ModeFilePath returns <DataDir>/mode.txt.
func ModeFilePath() (string, error) {
	d, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(d, "mode.txt"), nil
}

// EnvFilePath returns <DataDir>/.env.
func EnvFilePath() (string, error) {
	d, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(d, ".env"), nil
}
