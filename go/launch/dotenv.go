package launch

import (
	"os"
	"strings"
)

// MergeEnv parses path as KEY=value lines and returns os.Environ() overlaid with those keys.
// Missing file is ignored (returns copy of os.Environ()). Values are not quoted-shell-expanded.
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
