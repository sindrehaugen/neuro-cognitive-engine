package launch

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/nce/tri-stack/hardware"
)

// Run executes the mode-aware startup sequence until server.py exits or ctx is cancelled.
//
// For graceful shutdown (Phase 4 §6.4), pass a ctx created with signal.NotifyContext so SIGINT/SIGTERM
// cancel the root context: server.py and local-mode start_worker.py use CommandContext(ctx, …) and exit
// together without orphaned children in the normal case.
func Run(ctx context.Context, n UserNotifier, log *slog.Logger) error {
	if n == nil {
		n = LogNotifier{Log: log}
	}
	if log == nil {
		log = slog.Default()
	}

	if err := os.MkdirAll(mustLogDir(), 0o700); err != nil {
		return err
	}

	modePath, err := ModeFilePath()
	if err != nil {
		msg := "NCE data directory is not available."
		n.Error("NCE", msg)
		return err
	}

	mode, err := ReadMode(modePath)
	if err != nil {
		n.Error("NCE", "NCE mode is not set. Re-run the installer.")
		if log != nil {
			log.Warn("read_mode_failed", "err", err)
		}
		return err
	}

	envPath, err := EnvFilePath()
	if err != nil {
		return err
	}
	env, err := MergeEnv(envPath)
	if err != nil {
		n.Error("NCE", "Could not read your NCE configuration file.")
		if log != nil {
			log.Warn("merge_env_failed", "err", err)
		}
		return err
	}

	h, backend, hwErr := hardware.DetectAndPersistBackendIfUnset(envPath)
	if hwErr != nil && log != nil {
		log.Warn("nce_backend_env", "err", hwErr)
	}
	env = UpsertEnv(env, "NCE_BACKEND", backend)
	if log != nil {
		log.Info("hardware_snapshot",
			"nce_backend", backend,
			"cuda", h.CUDA,
			"rocm", h.ROCm,
			"intel_npu", h.IntelNPU,
			"intel_xpu", h.IntelXPU,
			"mps", h.MPS)
	}

	appRoot, err := AppRoot()
	if err != nil {
		n.Error("NCE", "Could not locate the NCE application folder.")
		return err
	}

	if log != nil {
		log.Info("nce_launch", "mode", string(mode), "app_root", appRoot)
	}

	switch mode {
	case ModeLocal:
		return runLocal(ctx, n, log, appRoot, env)
	case ModeMultiuser:
		return runMultiuser(ctx, n, log, appRoot, env)
	case ModeCloud:
		return runCloud(ctx, n, log, appRoot, env)
	default:
		return fmt.Errorf("unsupported mode %q", mode)
	}
}

func mustLogDir() string {
	d, err := LogDir()
	if err != nil {
		return filepath.Join(".", "logs")
	}
	return d
}

// SetupLogger returns a slog.Logger that appends to data-dir logs and optionally mirrors to w (e.g. stderr).
func SetupLogger(w io.Writer) (*slog.Logger, *os.File, error) {
	dir, err := LogDir()
	if err != nil {
		return nil, nil, err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, "nce-launch.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, nil, err
	}
	var h slog.Handler
	if w != nil {
		h = slog.NewJSONHandler(io.MultiWriter(f, w), &slog.HandlerOptions{Level: slog.LevelInfo})
	} else {
		h = slog.NewJSONHandler(f, &slog.HandlerOptions{Level: slog.LevelInfo})
	}
	return slog.New(h), f, nil
}
