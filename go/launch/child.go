package launch

import (
	"log/slog"
	"os/exec"
	"time"
)

func shutdownChild(log *slog.Logger, cmd *exec.Cmd, name string) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	if log != nil {
		log.Info("stopping_child", "name", name, "pid", cmd.Process.Pid)
	}
	_ = terminateGracefully(cmd.Process)

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil && log != nil {
			log.Debug("child_wait", "name", name, "err", err)
		}
	case <-time.After(childStopWait):
		_ = cmd.Process.Kill()
		<-done
	}
}
