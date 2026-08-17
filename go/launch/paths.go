package launch

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// DataDir returns %APPDATA%\NCE on Windows, ~/Library/Application Support/NCE on macOS, ~/.config/nce elsewhere.
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

// ModeFilePath is <DataDir>/mode.txt.
func ModeFilePath() (string, error) {
	d, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(d, "mode.txt"), nil
}

// EnvFilePath is <DataDir>/.env (NCE per-user config layered by installer).
func EnvFilePath() (string, error) {
	d, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(d, ".env"), nil
}

// LogDir is <DataDir>/logs.
func LogDir() (string, error) {
	d, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(d, "logs"), nil
}

// AppRoot returns the directory containing server.py, start_worker.py, and compose files.
// Resolution: NCE_APP_ROOT env, else directory of executable, else current working directory.
func AppRoot() (string, error) {
	if v := os.Getenv("NCE_APP_ROOT"); v != "" {
		return filepath.Clean(v), nil
	}
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	dir := filepath.Dir(exe)
	return dir, nil
}
