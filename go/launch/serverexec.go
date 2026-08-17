package launch

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
)

// runMCPServer runs server.py with stdin/stdout wired to the host process (Claude Desktop / Cursor MCP stdio).
// Stderr remains the host stderr so JSON-RPC stays clean on stdout while Python can still log to the log file
// or console as configured in server.py.
func runMCPServer(ctx context.Context, n UserNotifier, appRoot string, env []string, log *slog.Logger) error {
	if n == nil {
		n = LogNotifier{Log: log}
	}
	serverPy := filepath.Join(appRoot, "server.py")
	if _, err := os.Stat(serverPy); err != nil {
		n.Error("NCE", "NCE server script (server.py) is missing. Reinstall or repair the application.")
		return fmt.Errorf("server.py: %w", err)
	}
	py := pythonExe()
	scmd := exec.CommandContext(ctx, py, serverPy)
	scmd.Dir = appRoot
	scmd.Env = env
	scmd.Stdin = os.Stdin
	scmd.Stdout = os.Stdout
	scmd.Stderr = os.Stderr
	if err := scmd.Run(); err != nil {
		// User interrupt / SIGTERM cancels ctx; do not treat as an application failure or show an error dialog.
		if ctx.Err() != nil {
			if log != nil {
				log.Info("server_stopped", "reason", "shutdown")
			}
			return ctx.Err()
		}
		if log != nil {
			log.Warn("server_exit", "err", err)
		}
		n.Error("NCE", "The NCE server stopped unexpectedly. See the log file for details.")
		return err
	}
	return nil
}
