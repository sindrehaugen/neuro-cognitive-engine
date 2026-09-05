package launch

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// defaultAppRoot can be set at link time via:
// -ldflags "-X github.com/nce/tri-stack/launch.defaultAppRoot=/opt/nce"
var defaultAppRoot string

type envGetter func(string) string
type homeGetter func() (string, error)
type statChecker func(string) bool

func fileExists(path string) bool {
	info, err := os.Stat(path)
	if err != nil {
		return false
	}
	return !info.IsDir()
}

func isSystemInstall(goos string, getenv envGetter, exists statChecker) bool {
	if goos == "windows" || goos == "darwin" {
		return false
	}
	if v := getenv("NCE_SYSTEM_INSTALL"); v == "1" || v == "true" {
		return true
	}
	if exists != nil && exists("/etc/nce/mode.txt") {
		return true
	}
	return false
}

func resolveDataDir(goos string, getenv envGetter, getHome homeGetter, exists statChecker) (string, error) {
	if isSystemInstall(goos, getenv, exists) {
		return "/var/lib/nce", nil
	}

	switch goos {
	case "windows":
		appData := getenv("APPDATA")
		if appData == "" {
			return "", fmt.Errorf("APPDATA is not set")
		}
		return filepath.Join(appData, "NCE"), nil
	case "darwin":
		home, err := getHome()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, "Library", "Application Support", "NCE"), nil
	default:
		if xdgConfig := getenv("XDG_CONFIG_HOME"); xdgConfig != "" {
			return filepath.Join(xdgConfig, "nce"), nil
		}
		home, err := getHome()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, ".config", "nce"), nil
	}
}

func resolveModeFilePath(goos string, getenv envGetter, getHome homeGetter, exists statChecker) (string, error) {
	if isSystemInstall(goos, getenv, exists) {
		return "/etc/nce/mode.txt", nil
	}
	d, err := resolveDataDir(goos, getenv, getHome, exists)
	if err != nil {
		return "", err
	}
	return filepath.Join(d, "mode.txt"), nil
}

func resolveEnvFilePath(goos string, getenv envGetter, getHome homeGetter, exists statChecker) (string, error) {
	if isSystemInstall(goos, getenv, exists) {
		return "/etc/nce/nce.env", nil
	}
	d, err := resolveDataDir(goos, getenv, getHome, exists)
	if err != nil {
		return "", err
	}
	return filepath.Join(d, ".env"), nil
}

func resolveLogDir(goos string, getenv envGetter, getHome homeGetter, exists statChecker) (string, error) {
	if isSystemInstall(goos, getenv, exists) {
		return "/var/log/nce", nil
	}

	switch goos {
	case "windows":
		d, err := resolveDataDir(goos, getenv, getHome, exists)
		if err != nil {
			return "", err
		}
		return filepath.Join(d, "logs"), nil
	case "darwin":
		d, err := resolveDataDir(goos, getenv, getHome, exists)
		if err != nil {
			return "", err
		}
		return filepath.Join(d, "logs"), nil
	default:
		if xdgState := getenv("XDG_STATE_HOME"); xdgState != "" {
			return filepath.Join(xdgState, "nce", "logs"), nil
		}
		home, err := getHome()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, ".local", "state", "nce", "logs"), nil
	}
}

func resolveAppRoot(getenv envGetter, buildDefault string, exeGetter func() (string, error), wdGetter func() (string, error)) (string, error) {
	if v := getenv("NCE_APP_ROOT"); v != "" {
		return filepath.Clean(v), nil
	}
	if buildDefault != "" {
		return filepath.Clean(buildDefault), nil
	}
	if exeGetter != nil {
		exe, err := exeGetter()
		if err == nil && exe != "" {
			return filepath.Dir(exe), nil
		}
	}
	if wdGetter != nil {
		wd, err := wdGetter()
		if err == nil && wd != "" {
			return wd, nil
		}
	}
	return "", fmt.Errorf("could not resolve application root")
}

// DataDir returns %APPDATA%\NCE on Windows, ~/Library/Application Support/NCE on macOS,
// /var/lib/nce on Linux system installs, and $XDG_CONFIG_HOME/nce (default ~/.config/nce) on Linux per-user.
func DataDir() (string, error) {
	return resolveDataDir(runtime.GOOS, os.Getenv, os.UserHomeDir, fileExists)
}

// ModeFilePath returns /etc/nce/mode.txt on Linux system installs, else <DataDir>/mode.txt.
func ModeFilePath() (string, error) {
	return resolveModeFilePath(runtime.GOOS, os.Getenv, os.UserHomeDir, fileExists)
}

// EnvFilePath returns /etc/nce/nce.env on Linux system installs, else <DataDir>/.env.
func EnvFilePath() (string, error) {
	return resolveEnvFilePath(runtime.GOOS, os.Getenv, os.UserHomeDir, fileExists)
}

// LogDir returns /var/log/nce on Linux system installs, %APPDATA%\NCE\logs on Windows,
// ~/Library/Application Support/NCE/logs on macOS, and $XDG_STATE_HOME/nce/logs (default ~/.local/state/nce/logs) on Linux per-user.
func LogDir() (string, error) {
	return resolveLogDir(runtime.GOOS, os.Getenv, os.UserHomeDir, fileExists)
}

// AppRoot returns the directory containing server.py, start_worker.py, and compose files.
// Resolution order:
// 1. NCE_APP_ROOT environment variable (if set)
// 2. defaultAppRoot (set via build ldflags, if non-empty)
// 3. Directory of the current executable
// 4. Current working directory
func AppRoot() (string, error) {
	return resolveAppRoot(os.Getenv, defaultAppRoot, os.Executable, os.Getwd)
}
