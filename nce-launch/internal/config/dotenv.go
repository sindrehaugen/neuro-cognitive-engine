package config

import (
	"os"
	"strings"
)

// MergeEnv parses KEY=value lines from path and returns os.Environ() overlaid with those keys.
// Missing file is not an error (returns copy of os.Environ()). Lines support # comments; no shell expansion.
func MergeEnv(path string) ([]string, error) {
	base := os.Environ()
	if path == "" {
		return base, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return base, nil
		}
		return nil, err
	}
	patch := parseDotenv(raw)
	return mergeKeyValues(base, patch), nil
}

// ParseDotenv parses dotenv content into a map (for inspection without merging).
func ParseDotenv(raw []byte) map[string]string {
	return parseDotenv(raw)
}

func parseDotenv(raw []byte) map[string]string {
	out := make(map[string]string)
	for _, line := range strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.TrimSpace(v)
		if k == "" {
			continue
		}
		out[k] = v
	}
	return out
}

func mergeKeyValues(base []string, patch map[string]string) []string {
	idx := make(map[string]int)
	for i, kv := range base {
		k, _, ok := strings.Cut(kv, "=")
		if ok {
			idx[k] = i
		}
	}
	for k, v := range patch {
		kv := k + "=" + v
		if i, ok := idx[k]; ok {
			base[i] = kv
		} else {
			base = append(base, kv)
		}
	}
	return base
}
