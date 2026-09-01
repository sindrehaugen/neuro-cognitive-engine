package hardware

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Topology-aware persistence matrix (H6). DetectAndPersistBackendIfUnset must not
// write NCE_BACKEND when the chosen accelerator is host-visible but not reachable
// from the container that would consume it, because on the Python side a set
// NCE_BACKEND short-circuits NCE_COGNITIVE_BASE_URL and forces the model to load
// where the device is not. One row per test function, each with its own assert.

// withHardware installs a fixture snapshot for the duration of one test.
func withHardware(t *testing.T, h HardwareInfo) {
	t.Helper()
	prev := detectHardwareFn
	detectHardwareFn = func() HardwareInfo { return h }
	t.Cleanup(func() { detectHardwareFn = prev })
}

// snapshot walks the same path DetectHardware() uses: record the runtime that
// would consume the accelerator, then derive reachability from the real rules.
func snapshot(h HardwareInfo, runtime string, devs LinuxDeviceEvidence) HardwareInfo {
	h.ContainerRuntime = runtime
	h.AccelReachable = accelReachable(h, runtime, devs)
	return h
}

func tempEnvPath(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), ".env")
}

// readEnvFile returns "" when the file was never created.
func readEnvFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return ""
		}
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}

func winNPU() HardwareInfo {
	return snapshot(HardwareInfo{OS: "windows", Arch: "amd64", IntelNPU: true}, "docker-desktop-wsl2", LinuxDeviceEvidence{})
}

func linuxNPU() HardwareInfo {
	return snapshot(HardwareInfo{OS: "linux", Arch: "amd64", IntelNPU: true}, "docker-linux", LinuxDeviceEvidence{AccelNode: true})
}

// Row 1 - the defect. A host-visible NPU that no container on this box can see
// must leave the key absent so the remote sidecar wins.
func TestPersistIfUnset_HostSidecarWritesNoBackendKey(t *testing.T) {
	withHardware(t, winNPU())
	p := tempEnvPath(t)
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := readEnvFile(t, p); strings.Contains(got, "NCE_BACKEND") {
		t.Fatalf("host_sidecar must leave NCE_BACKEND unset, .env contains: %q", got)
	}
}

// Row 2 - /dev/accel makes the NPU container-reachable, so the key is written.
func TestPersistIfUnset_InContainerWritesBackend(t *testing.T) {
	withHardware(t, linuxNPU())
	p := tempEnvPath(t)
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := readEnvFile(t, p); !strings.Contains(got, "NCE_BACKEND=openvino_npu") {
		t.Fatalf("in_container must write openvino_npu, .env contains: %q", got)
	}
}

// Row 3 - H2 debt item 1: docker on PATH is not evidence of a containerised
// deployment. Mode local is native, so the host NPU is reached in process.
func TestPersistIfUnset_LocalModeIsNotContainerised(t *testing.T) {
	withHardware(t, winNPU())
	p := tempEnvPath(t)
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "local"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := readEnvFile(t, p); !strings.Contains(got, "NCE_BACKEND=openvino_npu") {
		t.Fatalf("local mode must write openvino_npu, .env contains: %q", got)
	}
}

// Row 4 - no accelerator at all: CPU is reachable everywhere.
func TestPersistIfUnset_NoAcceleratorWritesCPU(t *testing.T) {
	withHardware(t, snapshot(HardwareInfo{OS: "linux", Arch: "amd64"}, "docker-linux", LinuxDeviceEvidence{}))
	p := tempEnvPath(t)
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := readEnvFile(t, p); !strings.Contains(got, "NCE_BACKEND=cpu") {
		t.Fatalf("no accelerator must write cpu, .env contains: %q", got)
	}
}

// Row 5a - the manual override must survive the host_sidecar path.
func TestPersistIfUnset_ExistingBackendPreservedHostSidecar(t *testing.T) {
	withHardware(t, winNPU())
	p := tempEnvPath(t)
	if err := os.WriteFile(p, []byte("NCE_BACKEND=cuda\n"), 0o600); err != nil {
		t.Fatalf("seed .env: %v", err)
	}
	_, got, _, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "cuda" {
		t.Fatalf("existing NCE_BACKEND must win on host_sidecar, got %q", got)
	}
}

// Row 5b - and the manual override must survive the in_container path too.
func TestPersistIfUnset_ExistingBackendPreservedInContainer(t *testing.T) {
	withHardware(t, linuxNPU())
	p := tempEnvPath(t)
	if err := os.WriteFile(p, []byte("NCE_BACKEND=cuda\n"), 0o600); err != nil {
		t.Fatalf("seed .env: %v", err)
	}
	_, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := readEnvFile(t, p); !strings.Contains(got, "NCE_BACKEND=cuda") {
		t.Fatalf("existing NCE_BACKEND must not be overwritten, .env contains: %q", got)
	}
}

// Row 6 - a missing .env is created rather than reported as an error.
func TestPersistIfUnset_CreatesMissingEnvFile(t *testing.T) {
	withHardware(t, snapshot(HardwareInfo{OS: "linux", Arch: "amd64"}, "docker-linux", LinuxDeviceEvidence{}))
	p := tempEnvPath(t)
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, err := os.Stat(p); err != nil {
		t.Fatalf(".env was not created: %v", err)
	}
}

// countingHardware installs a fixture snapshot and returns a live counter of how
// many times the probe seam was invoked, so a test can assert it never ran.
func countingHardware(t *testing.T, h HardwareInfo) *int {
	t.Helper()
	prev := detectHardwareFn
	calls := 0
	detectHardwareFn = func() HardwareInfo { calls++; return h }
	t.Cleanup(func() { detectHardwareFn = prev })
	return &calls
}

// seedEnv writes a .env carrying exactly the given content.
func seedEnv(t *testing.T, path string, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("seed .env: %v", err)
	}
}

// Row 7 (H8) - the defect. When .env already carries NCE_BACKEND the ~8-10s host
// probe must not run at all, because its result is discarded.
func TestPersistIfUnset_ExistingBackendSkipsProbe(t *testing.T) {
	calls := countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_BACKEND=cuda\n")
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if *calls != 0 {
		t.Fatalf("detectHardwareFn must not be called when NCE_BACKEND is already set, called %d times", *calls)
	}
}

// Row 8 (H8) - the skipped path still returns the on-disk backend.
func TestPersistIfUnset_SkippedProbeReturnsExistingBackend(t *testing.T) {
	countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_BACKEND=cuda\n")
	_, got, _, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "cuda" {
		t.Fatalf("existing NCE_BACKEND must win, got %q", got)
	}
}

// Row 9 (H8) - and it reports detected=false so run.go cannot log a snapshot of
// zero values as if the hardware had been measured.
func TestPersistIfUnset_SkippedProbeReportsNotDetected(t *testing.T) {
	countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_BACKEND=cuda\n")
	_, _, detected, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if detected {
		t.Fatalf("detected must be false when the probe was skipped")
	}
}

// Row 10 (H8) - no NCE_BACKEND in an existing .env: the probe runs exactly once.
func TestPersistIfUnset_UnsetBackendProbesOnce(t *testing.T) {
	calls := countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_LOG_LEVEL=info\n")
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if *calls != 1 {
		t.Fatalf("probe must run exactly once when NCE_BACKEND is unset, called %d times", *calls)
	}
}

// Row 11 (H8) - and that path reports detected=true.
func TestPersistIfUnset_UnsetBackendReportsDetected(t *testing.T) {
	countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_LOG_LEVEL=info\n")
	_, _, detected, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !detected {
		t.Fatalf("detected must be true when the probe ran")
	}
}

// Row 12 (H8) - a missing .env is not an "already set" answer: probe once.
func TestPersistIfUnset_MissingEnvFileProbesOnce(t *testing.T) {
	calls := countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if *calls != 1 {
		t.Fatalf("probe must run once when .env does not exist, called %d times", *calls)
	}
}

// Row 13 (H8) - and the missing file is still created by the detected path.
func TestPersistIfUnset_MissingEnvFileStillCreated(t *testing.T) {
	countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	_, _, detected, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !detected {
		t.Fatalf("detected must be true when .env does not exist")
	}
	if got := readEnvFile(t, p); !strings.Contains(got, "NCE_BACKEND=openvino_npu") {
		t.Fatalf(".env was not created with the detected backend, contains: %q", got)
	}
}

// Row 14 (H8) - an empty NCE_BACKEND value is unset, so the probe runs.
func TestPersistIfUnset_EmptyBackendValueProbes(t *testing.T) {
	calls := countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_BACKEND=\n")
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if *calls != 1 {
		t.Fatalf("an empty NCE_BACKEND is unset and must probe once, called %d times", *calls)
	}
}

// Row 15 (H8) - a whitespace-only NCE_BACKEND is unset too (TrimSpace, pinned).
func TestPersistIfUnset_WhitespaceBackendValueProbes(t *testing.T) {
	calls := countingHardware(t, linuxNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_BACKEND=   \n")
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if *calls != 1 {
		t.Fatalf("a whitespace NCE_BACKEND is unset and must probe once, called %d times", *calls)
	}
}

// Row 16 (H8) - H6's host_sidecar rule must survive the reorder: with detection
// running, a host-visible but container-unreachable NPU still writes no key.
func TestPersistIfUnset_HostSidecarStillWritesNoKeyAfterReorder(t *testing.T) {
	countingHardware(t, winNPU())
	p := tempEnvPath(t)
	if _, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := readEnvFile(t, p); strings.Contains(got, "NCE_BACKEND") {
		t.Fatalf("host_sidecar must still leave NCE_BACKEND unset, .env contains: %q", got)
	}
}

// Row 17 (H8) - and the host_sidecar path reports detected=true, because it did
// probe; run.go relies on that to keep logging a real snapshot there.
func TestPersistIfUnset_HostSidecarReportsDetected(t *testing.T) {
	countingHardware(t, winNPU())
	p := tempEnvPath(t)
	_, _, detected, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !detected {
		t.Fatalf("detected must be true on the host_sidecar path")
	}
}

// Row 18 (H8) - run.go's guard: when detection is skipped h is zero-valued, so the
// topology resolves to in_process and the manual backend is injected into the child
// environment rather than suppressed. Assert it instead of assuming it.
func TestPersistIfUnset_SkippedProbeTopologyIsInProcess(t *testing.T) {
	countingHardware(t, winNPU())
	p := tempEnvPath(t)
	seedEnv(t, p, "NCE_BACKEND=cuda\n")
	h, _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := SuggestedTopology(h); got != "in_process" {
		t.Fatalf("skipped detection must resolve to in_process so run.go injects the manual backend, got %q", got)
	}
}
