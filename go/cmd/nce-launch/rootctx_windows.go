//go:build windows

package main

import (
	"context"
	"os"
	"os/signal"
)

// notifyRootContext cancels on Ctrl+C. Windows does not deliver SIGTERM to console apps
// the same way as Unix; service stops often use a different mechanism.
func notifyRootContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt)
}
