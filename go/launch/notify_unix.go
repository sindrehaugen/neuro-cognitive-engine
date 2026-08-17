//go:build !windows

package launch

import (
	"context"
	"fmt"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"

	"log/slog"
)

const notifyExecTimeout = 90 * time.Second

// PlatformNotifier uses osascript (macOS) or zenity (Linux) when available.
type PlatformNotifier struct {
	Log *slog.Logger
}

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
	ctx, cancel := context.WithTimeout(context.Background(), notifyExecTimeout)
	defer cancel()

	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		script := fmt.Sprintf(`display dialog %s with title %s buttons {"OK"} default button "OK" with icon stop`,
			strconv.Quote(message), strconv.Quote(title))
		cmd = exec.CommandContext(ctx, "osascript", "-e", script)
	default:
		if path, err := exec.LookPath("zenity"); err == nil && path != "" {
			cmd = exec.CommandContext(ctx, "zenity", "--error", "--title="+title, "--no-wrap", "--text="+message)
		} else if path, err := exec.LookPath("kdialog"); err == nil && path != "" {
			cmd = exec.CommandContext(ctx, "kdialog", "--error", message, "--title", title)
		}
	}
	if cmd == nil {
		return
	}
	cmd.Stdin = nil
	out, err := cmd.CombinedOutput()
	if err != nil && p.Log != nil {
		p.Log.Warn("dialog_failed", "err", err, "output", string(out))
	}
}

func (p PlatformNotifier) ConfirmConnectivity(title, message string) bool {
	if p.Log != nil {
		p.Log.Warn("user notification", "title", title, "message", message)
	}
	ctx, cancel := context.WithTimeout(context.Background(), notifyExecTimeout)
	defer cancel()

	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		script := fmt.Sprintf(`return button returned of (display dialog %s with title %s buttons {"No", "Yes"} default button "Yes" with icon question)`,
			strconv.Quote(message), strconv.Quote(title))
		dc := exec.CommandContext(ctx, "osascript", "-e", script)
		dc.Stdin = nil
		out, err := dc.Output()
		if err != nil {
			if p.Log != nil {
				p.Log.Warn("confirm_dialog_failed", "err", err)
			}
			return false
		}
		return strings.TrimSpace(string(out)) == "Yes"
	default:
		if path, err := exec.LookPath("zenity"); err == nil && path != "" {
			cmd = exec.CommandContext(ctx, "zenity", "--question", "--title="+title, "--no-wrap", "--text="+message)
		} else if path, err := exec.LookPath("kdialog"); err == nil && path != "" {
			cmd = exec.CommandContext(ctx, "kdialog", "--yesno", message, "--title", title)
		}
	}
	if cmd == nil {
		return false
	}
	cmd.Stdin = nil
	err := cmd.Run()
	if err != nil && p.Log != nil {
		p.Log.Warn("confirm_dialog_failed", "err", err)
	}
	// zenity/kdialog: exit 0 = Yes
	return err == nil
}
