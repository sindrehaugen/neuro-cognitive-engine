package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// NCEEnv models the subset of NCE .env keys the shim may rewrite after hardware detection (Phase 4).
type NCEEnv struct {
	NCE_BACKEND string
}

// Load parses known keys from an existing .env (best-effort; ignores comments and unknown keys).
func (e *NCEEnv) Load(path string) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	for _, line := range strings.Split(strings.ReplaceAll(string(b), "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch strings.TrimSpace(k) {
		case "NCE_BACKEND":
			e.NCE_BACKEND = strings.TrimSpace(v)
		}
	}
	return nil
}

// MergeIntoFile inserts or replaces NCE_BACKEND without discarding unrelated keys or comments.
// Creates parent directories and the file atomically when possible.
func (e *NCEEnv) MergeIntoFile(path string) error {
	if strings.TrimSpace(e.NCE_BACKEND) == "" {
		return fmt.Errorf("NCE_BACKEND is empty")
	}
	val := strings.TrimSpace(e.NCE_BACKEND)
	raw, err := os.ReadFile(path)
	var lines []string
	if err != nil {
		if !os.IsNotExist(err) {
			return err
		}
		lines = nil
	} else {
		lines = strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n")
	}

	const key = "NCE_BACKEND"
	prefix := key + "="
	replaced := false
OUTER:
	for i, line := range lines {
		t := strings.TrimSpace(line)
		if t == "" || strings.HasPrefix(t, "#") {
			continue
		}
		if strings.HasPrefix(t, prefix) {
			lines[i] = fmt.Sprintf("%s=%s", key, val)
			replaced = true
			break OUTER
		}
	}
	if !replaced {
		lines = append(lines, fmt.Sprintf("%s=%s", key, val))
	}

	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	payload := strings.Join(lines, "\n")
	if !strings.HasSuffix(payload, "\n") {
		payload += "\n"
	}
	if err := os.WriteFile(tmp, []byte(payload), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
