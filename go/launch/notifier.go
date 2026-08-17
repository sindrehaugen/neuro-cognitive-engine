package launch

import (
	"log/slog"
	"os"
)

// UserNotifier shows plain-language, native UI errors (Part 1 contract). No stack traces in dialogs.
type UserNotifier interface {
	// Error shows a blocking error dialog (or best-effort stderr in headless environments).
	Error(title, message string)
	// ConfirmConnectivity asks after a failed reachability check (e.g. Multi-User Postgres TCP).
	// Returns true if the user wants to retry (e.g. VPN was connected).
	ConfirmConnectivity(title, message string) bool
}

// LogNotifier logs only (CI / headless fallback).
type LogNotifier struct {
	Log *slog.Logger
}

func (l LogNotifier) Error(title, message string) {
	if l.Log != nil {
		l.Log.Error("user notification", "title", title, "message", message)
		return
	}
	_, _ = os.Stderr.WriteString(title + ": " + message + "\n")
}

func (l LogNotifier) ConfirmConnectivity(title, message string) bool {
	if l.Log != nil {
		l.Log.Warn("connectivity prompt (no UI)", "title", title, "message", message)
	}
	return false
}
