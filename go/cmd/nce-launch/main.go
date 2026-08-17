// Command nce-launch is the mode-aware MCP stdio shim (NCE Enterprise Deployment Plan §6.4).
package main

import (
	"context"
	"errors"
	"log/slog"
	"os"

	"github.com/nce/tri-stack/launch"
)

func main() {
	log, f, err := launch.SetupLogger(os.Stderr)
	if err != nil {
		_, _ = os.Stderr.WriteString("nce-launch: logger: " + err.Error() + "\n")
		os.Exit(1)
	}
	if f != nil {
		defer func() { _ = f.Close() }()
	}

	ctx, stop := notifyRootContext()
	defer stop()

	// SIGTERM forwarding to MCP child processes is handled inside
	// github.com/nce/tri-stack/launch.Run — behaviour is not verifiable from this repository alone (FIX-046).
	n := launch.NewPlatformNotifier(log)
	if err := launch.Run(ctx, n, log); err != nil {
		if errors.Is(err, context.Canceled) {
			os.Exit(0)
		}
		log.Error("launch_failed", slog.String("err", err.Error()))
		os.Exit(1)
	}
}
