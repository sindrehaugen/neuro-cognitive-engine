package launch

import (
	"path/filepath"
	"testing"
)

func TestPaths_Matrix(t *testing.T) {
	tests := []struct {
		name         string
		goos         string
		env          map[string]string
		home         string
		homeErr      error
		fileExists   func(string) bool
		wantDataDir  string
		wantModeFile string
		wantEnvFile  string
		wantLogDir   string
	}{
		{
			name: "windows_standard",
			goos: "windows",
			env: map[string]string{
				"APPDATA": `C:\Users\test\AppData\Roaming`,
			},
			home:         `C:\Users\test`,
			wantDataDir:  filepath.Join(`C:\Users\test\AppData\Roaming`, "NCE"),
			wantModeFile: filepath.Join(`C:\Users\test\AppData\Roaming`, "NCE", "mode.txt"),
			wantEnvFile:  filepath.Join(`C:\Users\test\AppData\Roaming`, "NCE", ".env"),
			wantLogDir:   filepath.Join(`C:\Users\test\AppData\Roaming`, "NCE", "logs"),
		},
		{
			name:         "darwin_standard",
			goos:         "darwin",
			env:          map[string]string{},
			home:         "/Users/test",
			wantDataDir:  filepath.Join("/Users/test", "Library", "Application Support", "NCE"),
			wantModeFile: filepath.Join("/Users/test", "Library", "Application Support", "NCE", "mode.txt"),
			wantEnvFile:  filepath.Join("/Users/test", "Library", "Application Support", "NCE", ".env"),
			wantLogDir:   filepath.Join("/Users/test", "Library", "Application Support", "NCE", "logs"),
		},
		{
			name:         "linux_user_default",
			goos:         "linux",
			env:          map[string]string{},
			home:         "/home/test",
			wantDataDir:  filepath.Join("/home/test", ".config", "nce"),
			wantModeFile: filepath.Join("/home/test", ".config", "nce", "mode.txt"),
			wantEnvFile:  filepath.Join("/home/test", ".config", "nce", ".env"),
			wantLogDir:   filepath.Join("/home/test", ".local", "state", "nce", "logs"),
		},
		{
			name: "linux_user_xdg_custom",
			goos: "linux",
			env: map[string]string{
				"XDG_CONFIG_HOME": "/custom/config",
				"XDG_STATE_HOME":  "/custom/state",
			},
			home:         "/home/test",
			wantDataDir:  filepath.Join("/custom/config", "nce"),
			wantModeFile: filepath.Join("/custom/config", "nce", "mode.txt"),
			wantEnvFile:  filepath.Join("/custom/config", "nce", ".env"),
			wantLogDir:   filepath.Join("/custom/state", "nce", "logs"),
		},
		{
			name: "linux_system_install_via_env",
			goos: "linux",
			env: map[string]string{
				"NCE_SYSTEM_INSTALL": "1",
			},
			home:         "/root",
			wantDataDir:  "/var/lib/nce",
			wantModeFile: "/etc/nce/mode.txt",
			wantEnvFile:  "/etc/nce/nce.env",
			wantLogDir:   "/var/log/nce",
		},
		{
			name: "linux_system_install_via_file",
			goos: "linux",
			env:  map[string]string{},
			home: "/root",
			fileExists: func(path string) bool {
				return path == "/etc/nce/mode.txt"
			},
			wantDataDir:  "/var/lib/nce",
			wantModeFile: "/etc/nce/mode.txt",
			wantEnvFile:  "/etc/nce/nce.env",
			wantLogDir:   "/var/log/nce",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			getenv := func(key string) string {
				return tt.env[key]
			}
			getHome := func() (string, error) {
				if tt.homeErr != nil {
					return "", tt.homeErr
				}
				return tt.home, nil
			}

			dataDir, err := resolveDataDir(tt.goos, getenv, getHome, tt.fileExists)
			if err != nil {
				t.Fatalf("resolveDataDir failed: %v", err)
			}
			if dataDir != tt.wantDataDir {
				t.Errorf("resolveDataDir = %q, want %q", dataDir, tt.wantDataDir)
			}

			modeFile, err := resolveModeFilePath(tt.goos, getenv, getHome, tt.fileExists)
			if err != nil {
				t.Fatalf("resolveModeFilePath failed: %v", err)
			}
			if modeFile != tt.wantModeFile {
				t.Errorf("resolveModeFilePath = %q, want %q", modeFile, tt.wantModeFile)
			}

			envFile, err := resolveEnvFilePath(tt.goos, getenv, getHome, tt.fileExists)
			if err != nil {
				t.Fatalf("resolveEnvFilePath failed: %v", err)
			}
			if envFile != tt.wantEnvFile {
				t.Errorf("resolveEnvFilePath = %q, want %q", envFile, tt.wantEnvFile)
			}

			logDir, err := resolveLogDir(tt.goos, getenv, getHome, tt.fileExists)
			if err != nil {
				t.Fatalf("resolveLogDir failed: %v", err)
			}
			if logDir != tt.wantLogDir {
				t.Errorf("resolveLogDir = %q, want %q", logDir, tt.wantLogDir)
			}
		})
	}
}

func TestAppRoot_ResolutionOrder(t *testing.T) {
	t.Run("env_override", func(t *testing.T) {
		getenv := func(k string) string {
			if k == "NCE_APP_ROOT" {
				return "/custom/app/root"
			}
			return ""
		}
		root, err := resolveAppRoot(getenv, "/opt/nce", func() (string, error) {
			return "/usr/bin/nce-launch", nil
		}, func() (string, error) {
			return "/home/user", nil
		})
		if err != nil {
			t.Fatalf("resolveAppRoot failed: %v", err)
		}
		if root != filepath.Clean("/custom/app/root") {
			t.Errorf("expected /custom/app/root, got %q", root)
		}
	})

	t.Run("build_default_override", func(t *testing.T) {
		getenv := func(k string) string { return "" }
		root, err := resolveAppRoot(getenv, "/opt/nce", func() (string, error) {
			return "/usr/bin/nce-launch", nil
		}, func() (string, error) {
			return "/home/user", nil
		})
		if err != nil {
			t.Fatalf("resolveAppRoot failed: %v", err)
		}
		if root != filepath.Clean("/opt/nce") {
			t.Errorf("expected /opt/nce, got %q", root)
		}
	})

	t.Run("exe_dir_fallback", func(t *testing.T) {
		getenv := func(k string) string { return "" }
		root, err := resolveAppRoot(getenv, "", func() (string, error) {
			return "/opt/nce-portable/nce-launch", nil
		}, func() (string, error) {
			return "/home/user", nil
		})
		if err != nil {
			t.Fatalf("resolveAppRoot failed: %v", err)
		}
		if root != filepath.Clean("/opt/nce-portable") {
			t.Errorf("expected /opt/nce-portable, got %q", root)
		}
	})

	t.Run("cwd_fallback", func(t *testing.T) {
		getenv := func(k string) string { return "" }
		root, err := resolveAppRoot(getenv, "", func() (string, error) {
			return "", nil
		}, func() (string, error) {
			return "/var/www/app", nil
		})
		if err != nil {
			t.Fatalf("resolveAppRoot failed: %v", err)
		}
		if root != filepath.Clean("/var/www/app") {
			t.Errorf("expected /var/www/app, got %q", root)
		}
	})
}
