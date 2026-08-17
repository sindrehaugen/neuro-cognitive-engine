// Package executil runs external probes with mandatory deadlines so the shim cannot hang on boot.
package executil

import (
	"context"
	"os/exec"
	"runtime"
)

// Output runs cmd with ctx cancellation (process is killed when ctx expires).
func Output(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	// Isolate child process groups on POSIX so timeouts kill the full tree where supported.
	if runtime.GOOS != "windows" {
		setSysProcAttr(cmd)
	}
	return cmd.Output()
}

// Run is like Output but does not capture stdout (for probes that only need exit code).
func Run(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	if runtime.GOOS != "windows" {
		setSysProcAttr(cmd)
	}
	return cmd.Run()
}
