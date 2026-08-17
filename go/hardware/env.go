package hardware

import (
	"os"
	"strings"

	ncecfg "github.com/nce/tri-stack/config"
)

// DetectAndPersistBackend runs §8.4 detection, picks NCE_BACKEND fallback order matching Python,
// and merges the choice into the user .env (e.g. %APPDATA%\NCE\.env).
func DetectAndPersistBackend(dotenvPath string) (HardwareInfo, string, error) {
	h := DetectHardware()
	b := SuggestedBackend(h)
	env := ncecfg.NCEEnv{NCE_BACKEND: b}
	if err := env.MergeIntoFile(dotenvPath); err != nil {
		return h, b, err
	}
	return h, b, nil
}

// DetectAndPersistBackendIfUnset runs §8.4 detection and writes NCE_BACKEND only when the
// key is missing or the file does not exist (§6.2 wizard manual override preserved).
// On .env read error (other than not found), returns err but still returns suggested backend for in-process use.
func DetectAndPersistBackendIfUnset(dotenvPath string) (HardwareInfo, string, error) {
	h := DetectHardware()
	suggested := SuggestedBackend(h)

	var cur ncecfg.NCEEnv
	loadErr := cur.Load(dotenvPath)
	if loadErr != nil && !os.IsNotExist(loadErr) {
		return h, suggested, loadErr
	}
	if loadErr == nil {
		if ex := strings.TrimSpace(cur.NCE_BACKEND); ex != "" {
			return h, ex, nil
		}
	}
	env := &ncecfg.NCEEnv{NCE_BACKEND: suggested}
	if werr := env.MergeIntoFile(dotenvPath); werr != nil {
		return h, suggested, werr
	}
	return h, suggested, nil
}
