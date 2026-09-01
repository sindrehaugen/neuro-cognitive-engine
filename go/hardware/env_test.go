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
	if _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
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
	if _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
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
	if _, _, err := DetectAndPersistBackendIfUnset(p, "local"); err != nil {
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
	if _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
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
	_, got, err := DetectAndPersistBackendIfUnset(p, "multiuser")
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
	_, _, err := DetectAndPersistBackendIfUnset(p, "multiuser")
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
	if _, _, err := DetectAndPersistBackendIfUnset(p, "multiuser"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, err := os.Stat(p); err != nil {
		t.Fatalf(".env was not created: %v", err)
	}
}
