//go:build !windows

package launch

import (
	"os"
	"syscall"
)

func terminateGracefully(p *os.Process) error {
	if p == nil {
		return nil
	}
	return p.Signal(syscall.SIGTERM)
}
