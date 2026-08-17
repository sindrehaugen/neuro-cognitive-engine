//go:build windows

package launch

import "os"

func terminateGracefully(p *os.Process) error {
	if p == nil {
		return nil
	}
	// Windows has no reliable POSIX SIGTERM for arbitrary children; TerminateProcess is best-effort.
	return p.Kill()
}
