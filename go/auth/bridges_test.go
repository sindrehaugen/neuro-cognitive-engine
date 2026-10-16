package auth

import (
	"path/filepath"
	"testing"
)

func TestResolveNCEDataDir_Matrix(t *testing.T) {
	base := "/home/user/.config"
	lower := filepath.Join(base, "nce")
	upper := filepath.Join(base, "NCE")

	tests := []struct {
		name     string
		goos     string
		existing map[string]bool
		want     string
	}{
		{
			name:     "linux_default_prefers_lowercase_nce",
			goos:     "linux",
			existing: map[string]bool{},
			want:     lower,
		},
		{
			name:     "linux_lowercase_exists",
			goos:     "linux",
			existing: map[string]bool{lower: true},
			want:     lower,
		},
		{
			name:     "linux_uppercase_fallback_when_lowercase_absent",
			goos:     "linux",
			existing: map[string]bool{upper: true},
			want:     upper,
		},
		{
			name:     "linux_both_exist_prefers_lowercase",
			goos:     "linux",
			existing: map[string]bool{lower: true, upper: true},
			want:     lower,
		},
		{
			name:     "windows_uses_uppercase",
			goos:     "windows",
			existing: map[string]bool{},
			want:     upper,
		},
		{
			name:     "darwin_uses_uppercase",
			goos:     "darwin",
			existing: map[string]bool{},
			want:     upper,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := resolveNCEDataDir(tt.goos, base, func(p string) bool {
				return tt.existing[p]
			})
			if got != tt.want {
				t.Fatalf("resolveNCEDataDir(%q) = %q, want %q", tt.goos, got, tt.want)
			}
		})
	}
}

func TestNCEDataDir_NonEmpty(t *testing.T) {
	dir, err := NCEDataDir()
	if err != nil {
		t.Fatalf("NCEDataDir failed: %v", err)
	}
	if dir == "" {
		t.Fatal("expected non-empty NCEDataDir")
	}
}
