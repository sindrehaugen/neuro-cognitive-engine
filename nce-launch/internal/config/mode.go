package config

import (
	"fmt"
	"os"
	"strings"
)

// DeployMode is the NCE deployment mode from mode.txt (§6.4).
type DeployMode string

const (
	ModeLocal     DeployMode = "local"
	ModeMultiuser DeployMode = "multiuser"
	ModeCloud     DeployMode = "cloud"
)

// ReadMode reads and normalizes mode.txt (trim, lowercase).
func ReadMode(path string) (DeployMode, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read mode file %s: %w", path, err)
	}
	s := strings.ToLower(strings.TrimSpace(string(b)))
	switch s {
	case "local":
		return ModeLocal, nil
	case "multiuser", "multi-user", "multi_user":
		return ModeMultiuser, nil
	case "cloud":
		return ModeCloud, nil
	default:
		return "", fmt.Errorf("unknown mode %q in %s (expected local, multiuser, cloud)", s, path)
	}
}
