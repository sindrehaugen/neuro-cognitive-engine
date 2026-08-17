//go:build windows

package launch

import (
	"context"
	"os/exec"
	"strings"
	"time"

	"log/slog"
)

const notifyExecTimeout = 90 * time.Second

// PlatformNotifier shows a native WinForms message box via PowerShell (no extra deps).
type PlatformNotifier struct {
	Log *slog.Logger
}

// NewPlatformNotifier returns a notifier suitable for the current OS.
func NewPlatformNotifier(log *slog.Logger) UserNotifier {
	if log == nil {
		log = slog.Default()
	}
	return PlatformNotifier{Log: log}
}

func (p PlatformNotifier) Error(title, message string) {
	if p.Log != nil {
		p.Log.Error("user notification", "title", title, "message", message)
	}
	esc := func(s string) string {
		return strings.ReplaceAll(strings.ReplaceAll(s, "`", "``"), "'", "''")
	}
	ps := "Add-Type -AssemblyName System.Windows.Forms; " +
		"[System.Windows.Forms.MessageBox]::Show('" + esc(message) + "', '" + esc(title) + "', " +
		"[System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)"
	ctx, cancel := context.WithTimeout(context.Background(), notifyExecTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	cmd.Stdin = nil
	out, err := cmd.CombinedOutput()
	if err != nil && p.Log != nil {
		p.Log.Warn("messagebox_failed", "err", err, "output", string(out))
	}
}

func (p PlatformNotifier) ConfirmConnectivity(title, message string) bool {
	if p.Log != nil {
		p.Log.Warn("user notification", "title", title, "message", message)
	}
	esc := func(s string) string {
		return strings.ReplaceAll(strings.ReplaceAll(s, "`", "``"), "'", "''")
	}
	ps := "Add-Type -AssemblyName System.Windows.Forms; " +
		"$r = [System.Windows.Forms.MessageBox]::Show('" + esc(message) + "', '" + esc(title) + "', " +
		"[System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Question); " +
		"if ($r -eq [System.Windows.Forms.DialogResult]::Yes) { exit 0 } else { exit 1 }"
	ctx, cancel := context.WithTimeout(context.Background(), notifyExecTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	cmd.Stdin = nil
	out, err := cmd.CombinedOutput()
	if err != nil && p.Log != nil {
		p.Log.Warn("messagebox_confirm_failed", "err", err, "output", string(out))
		return false
	}
	return true
}
