//go:build !windows

package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"
)

// notifyRootContext cancels on Ctrl+C (SIGINT) or SIGTERM (service stop, systemd, launchd).
func notifyRootContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}
