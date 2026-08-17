package logx

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/nce/tri-stack/nce-launch/internal/paths"
)

// Setup creates <LogDir>/nce-launch.log, ensures directories exist, and returns
// a logger that writes JSON lines to the file and optional human-readable lines to w.
func Setup(w io.Writer) (*slog.Logger, *os.File, error) {
	dir, err := paths.LogDir()
	if err != nil {
		return nil, nil, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, nil, err
	}
	logPath := filepath.Join(dir, "nce-launch.log")
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, nil, err
	}
	fileHandler := slog.NewJSONHandler(f, &slog.HandlerOptions{Level: slog.LevelDebug})
	var h slog.Handler = fileHandler
	if w != nil {
		stderrHandler := slog.NewTextHandler(w, &slog.HandlerOptions{Level: slog.LevelInfo})
		h = newTeeHandler(fileHandler, stderrHandler)
	}
	return slog.New(h), f, nil
}

type teeHandler struct {
	file   slog.Handler
	stderr slog.Handler
}

func newTeeHandler(file, stderr slog.Handler) *teeHandler {
	return &teeHandler{file: file, stderr: stderr}
}

func (t *teeHandler) Enabled(ctx context.Context, level slog.Level) bool {
	if t.file.Enabled(ctx, level) {
		return true
	}
	return t.stderr != nil && t.stderr.Enabled(ctx, level)
}

func (t *teeHandler) Handle(ctx context.Context, r slog.Record) error {
	if err := t.file.Handle(ctx, r.Clone()); err != nil {
		return err
	}
	if t.stderr != nil && t.stderr.Enabled(ctx, r.Level) {
		return t.stderr.Handle(ctx, r.Clone())
	}
	return nil
}

func (t *teeHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	var s slog.Handler
	if t.stderr != nil {
		s = t.stderr.WithAttrs(attrs)
	}
	return newTeeHandler(t.file.WithAttrs(attrs), s)
}

func (t *teeHandler) WithGroup(name string) slog.Handler {
	var s slog.Handler
	if t.stderr != nil {
		s = t.stderr.WithGroup(name)
	}
	return newTeeHandler(t.file.WithGroup(name), s)
}
