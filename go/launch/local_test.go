package launch

import (
	"os"
	"path/filepath"
	"testing"
)

func TestResolveComposeFile_RelativeOverridePrefersAppRoot(t *testing.T) {
	appRoot := t.TempDir()
	cwdDir := t.TempDir()

	appCompose := filepath.Join(appRoot, "custom-compose.yml")
	if err := os.WriteFile(appCompose, []byte("services: {}\n"), 0o600); err != nil {
		t.Fatalf("write appCompose: %v", err)
	}
	cwdCompose := filepath.Join(cwdDir, "custom-compose.yml")
	if err := os.WriteFile(cwdCompose, []byte("services: {}\n"), 0o600); err != nil {
		t.Fatalf("write cwdCompose: %v", err)
	}

	origWD, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	if err := os.Chdir(cwdDir); err != nil {
		t.Fatalf("chdir: %v", err)
	}
	t.Cleanup(func() {
		_ = os.Chdir(origWD)
	})

	t.Setenv("NCE_COMPOSE_FILE", "  custom-compose.yml  ")

	got := resolveComposeFile(appRoot)
	if got != appCompose {
		t.Fatalf("resolveComposeFile = %q, want %q (under appRoot)", got, appCompose)
	}
}

func TestResolveComposeFile_RelativeOverrideFallsBackToCWD(t *testing.T) {
	appRoot := t.TempDir()
	cwdDir := t.TempDir()

	cwdCompose := filepath.Join(cwdDir, "cwd-only-compose.yml")
	if err := os.WriteFile(cwdCompose, []byte("services: {}\n"), 0o600); err != nil {
		t.Fatalf("write cwdCompose: %v", err)
	}

	origWD, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	if err := os.Chdir(cwdDir); err != nil {
		t.Fatalf("chdir: %v", err)
	}
	t.Cleanup(func() {
		_ = os.Chdir(origWD)
	})

	t.Setenv("NCE_COMPOSE_FILE", "cwd-only-compose.yml")

	got := resolveComposeFile(appRoot)
	if got != "cwd-only-compose.yml" {
		t.Fatalf("resolveComposeFile = %q, want %q (CWD fallback)", got, "cwd-only-compose.yml")
	}
}

func TestResolveComposeFile_AbsoluteOverride(t *testing.T) {
	appRoot := t.TempDir()
	absPath := filepath.Join(t.TempDir(), "abs-compose.yml")
	t.Setenv("NCE_COMPOSE_FILE", absPath)

	got := resolveComposeFile(appRoot)
	if got != absPath {
		t.Fatalf("resolveComposeFile = %q, want %q", got, absPath)
	}
}
