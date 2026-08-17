package launch

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/nce/tri-stack/internal/executil"
)

func runLocal(ctx context.Context, n UserNotifier, log *slog.Logger, appRoot string, env []string) error {
	dctx, cancel := context.WithTimeout(ctx, dockerProbeTimeout)
	defer cancel()
	if err := executil.Run(dctx, "docker", "info"); err != nil {
		msg := "Docker does not appear to be running. Start Docker Desktop and try again."
		n.Error("NCE", msg)
		if log != nil {
			log.Warn("docker_probe_failed", "err", err)
		}
		return fmt.Errorf("docker: %w", err)
	}

	composeFile := resolveComposeFile(appRoot)
	if _, err := os.Stat(composeFile); err != nil {
		msg := fmt.Sprintf("Compose file not found: %s", filepath.Base(composeFile))
		n.Error("NCE", msg)
		return err
	}

	cctx, ccancel := context.WithTimeout(ctx, composeUpTimeout)
	defer ccancel()
	ccmd := exec.CommandContext(cctx, "docker", "compose", "-f", composeFile, "up", "-d", "--wait")
	ccmd.Dir = appRoot
	ccmd.Env = env
	ccmd.Stdout = os.Stderr // compose progress to stderr; keep stdout for MCP
	ccmd.Stderr = os.Stderr
	if err := ccmd.Run(); err != nil {
		msg := "Could not start local NCE containers. See log file for details."
		n.Error("NCE", msg)
		if log != nil {
			log.Warn("compose_up_failed", "err", err)
		}
		return fmt.Errorf("docker compose: %w", err)
	}

	py := pythonExe()
	workerPy := filepath.Join(appRoot, "start_worker.py")
	serverPy := filepath.Join(appRoot, "server.py")
	for _, p := range []struct{ path, label string }{{serverPy, "server.py"}, {workerPy, "start_worker.py"}} {
		if _, err := os.Stat(p.path); err != nil {
			msg := fmt.Sprintf("Missing %s under application folder.", p.label)
			n.Error("NCE", msg)
			return err
		}
	}

	logDir, err := LogDir()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(logDir, 0o700); err != nil {
		return err
	}
	wlog, err := os.OpenFile(filepath.Join(logDir, "worker.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer func() { _ = wlog.Close() }()

	// Worker tied to root ctx so SIGINT/SIGTERM (NotifyContext) kills it if the server dies first.
	wcmd := exec.CommandContext(ctx, py, workerPy)
	wcmd.Dir = appRoot
	wcmd.Env = env
	wcmd.Stdout = wlog
	wcmd.Stderr = wlog
	if err := wcmd.Start(); err != nil {
		msg := "Could not start the background worker process."
		n.Error("NCE", msg)
		return fmt.Errorf("start_worker: %w", err)
	}
	defer func() {
		shutdownChild(log, wcmd, "worker")
	}()

	return runMCPServer(ctx, n, appRoot, env, log)
}

func resolveComposeFile(appRoot string) string {
	if v := os.Getenv("NCE_COMPOSE_FILE"); v != "" {
		return v
	}
	for _, name := range []string{"docker-compose.local.yml", "docker-compose.yml"} {
		p := filepath.Join(appRoot, name)
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return filepath.Join(appRoot, "docker-compose.local.yml")
}

func pythonExe() string {
	if v := os.Getenv("NCE_PYTHON"); v != "" {
		return v
	}
	return "python"
}
