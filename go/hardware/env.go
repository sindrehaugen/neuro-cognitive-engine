package hardware

import (
	"log/slog"
	"os"
	"strings"

	ncecfg "github.com/nce/tri-stack/config"
)

// detectHardwareFn is the injection seam for the persistence rules below: tests
// substitute a fixture HardwareInfo so every topology row runs with no hardware.
// Production always uses the real host probe. It is a seam, not an abstraction.
var detectHardwareFn = DetectHardware

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
// The .env is loaded FIRST: when NCE_BACKEND is already present and non-empty the
// hardware probe is skipped entirely and the returned detected flag is false, so a
// caller never logs a zero-valued snapshot as if it had been measured.
// On .env read error (other than not found), returns err but still returns suggested backend for in-process use.
// deployMode is the <DataDir>/mode.txt value ("local" | "multiuser" | "cloud").
// Mode "local" is a native run, so docker merely being installed on the host does
// not make the deployment containerised and the host device is reached directly.
// When the topology is host_sidecar the accelerator is host-visible but invisible
// to the container that would consume it; NCE_BACKEND is then deliberately left
// unwritten, because on the Python side a set NCE_BACKEND short-circuits
// NCE_COGNITIVE_BASE_URL and would force the model to load where the device is not.
func DetectAndPersistBackendIfUnset(dotenvPath string, deployMode string) (HardwareInfo, string, bool, error) {
	var cur ncecfg.NCEEnv
	loadErr := cur.Load(dotenvPath)
	if loadErr == nil {
		if ex := strings.TrimSpace(cur.NCE_BACKEND); ex != "" {
			// The answer is already on disk and a probe result would be discarded,
			// so do not pay the ~8-10s host probe on every launch. detected=false
			// tells the caller the zero-valued HardwareInfo was never measured.
			return HardwareInfo{}, ex, false, nil
		}
	}
	h := detectHardwareFn()
	if strings.EqualFold(strings.TrimSpace(deployMode), "local") {
		h.ContainerRuntime = ""
	}
	suggested := SuggestedBackend(h)
	topology := SuggestedTopology(h)

	if loadErr != nil && !os.IsNotExist(loadErr) {
		// Unchanged contract: a real read error is returned together with a
		// suggested backend for in-process use, which requires the probe.
		return h, suggested, true, loadErr
	}
	if topology == "host_sidecar" {
		slog.Info("nce_backend_left_unset",
			"reason", "accelerator is host-visible but not container-reachable",
			"suggested_backend", suggested,
			"topology", topology,
			"container_runtime", h.ContainerRuntime,
			"deploy_mode", deployMode)
		return h, suggested, true, nil
	}
	env := &ncecfg.NCEEnv{NCE_BACKEND: suggested}
	if werr := env.MergeIntoFile(dotenvPath); werr != nil {
		return h, suggested, true, werr
	}
	return h, suggested, true, nil
}
