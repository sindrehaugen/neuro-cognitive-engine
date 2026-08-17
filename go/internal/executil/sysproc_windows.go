//go:build windows

package executil

import "os/exec"

func setSysProcAttr(_ *exec.Cmd) {}
