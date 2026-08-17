package launch

import "strings"

// LookupEnv returns the last value for key in a list of KEY=value pairs (os.Environ layout).
func LookupEnv(env []string, key string) string {
	prefix := key + "="
	for i := len(env) - 1; i >= 0; i-- {
		if strings.HasPrefix(env[i], prefix) {
			return strings.TrimPrefix(env[i], prefix)
		}
	}
	return ""
}

// UpsertEnv drops every prior KEY= entry for key and appends key=value once (canonical for child env).
func UpsertEnv(env []string, key, value string) []string {
	prefix := key + "="
	out := make([]string, 0, len(env)+1)
	for _, kv := range env {
		if strings.HasPrefix(kv, prefix) {
			continue
		}
		out = append(out, kv)
	}
	return append(out, key+"="+value)
}
