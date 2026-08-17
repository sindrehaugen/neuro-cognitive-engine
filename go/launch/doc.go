// Package launch implements nce-launch orchestration (NCE Enterprise Deployment Plan §6.4).
//
// Orphan-process note (RCA): SIGKILL (kill -9), “End task”, or taskkill /F cannot run deferred cleanup
// or signal handlers. Child Python processes (server.py, start_worker.py) and Docker containers started
// by the shim may then keep running until the OS reparents them (init) or the user stops them manually.
// Normal SIGINT/SIGTERM flows cancel the root context: server.py (CommandContext) and start_worker.py
// (CommandContext) are terminated, then local mode’s shutdownChild waits briefly and force-kills if needed.
// Containers are not torn down on graceful exit here; IT may use docker compose down separately.
// Stronger containment (optional future work): POSIX PR_SET_PDEATHSIG, process groups, Windows Job Objects.
package launch
