package launch

import "time"

const (
	dockerProbeTimeout = 5 * time.Second
	composeUpTimeout   = 300 * time.Second
	pgTCPTimeout       = 6 * time.Second
	pgTLSTimeout       = 10 * time.Second
	childStopWait      = 8 * time.Second
)
