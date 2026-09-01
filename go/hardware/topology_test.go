package hardware

import "testing"

// D43 fixture matrix. DetectHardware() runs on the HOST, never inside a container,
// so reachability is inferred from (host OS, container runtime, accelerator kind)
// plus Linux device nodes — which is what lets every row below run with no
// hardware. One row per test function, each with its own assert, so a failure
// names the rule that broke.

// resolve walks the exact decision path DetectHardware() uses: populate
// ContainerRuntime, derive AccelReachable from it, then read the topology off the
// populated struct.
func resolve(h HardwareInfo, runtime string, devs LinuxDeviceEvidence) HardwareInfo {
	h.ContainerRuntime = runtime
	h.AccelReachable = accelReachable(h, runtime, devs)
	return h
}

// Row 1 — the dev host: NPU present, healthy, and invisible to every container.
func TestWindowsNPUUnderWSL2IsHostSidecar(t *testing.T) {
	got := resolve(HardwareInfo{OS: "windows", Arch: "amd64", IntelNPU: true}, "docker-desktop-wsl2", LinuxDeviceEvidence{})
	if got.AccelReachable {
		t.Fatalf("windows IntelNPU under WSL2: AccelReachable = true, want false (no /dev/accel in WSL2)")
	}
	if topo := SuggestedTopology(got); topo != "host_sidecar" {
		t.Fatalf("windows IntelNPU under WSL2: topology = %q, want host_sidecar", topo)
	}
}

// Row 2 — WSL2 does pass CUDA through.
func TestWindowsCUDAUnderWSL2IsInContainer(t *testing.T) {
	got := resolve(HardwareInfo{OS: "windows", Arch: "amd64", CUDA: true}, "docker-desktop-wsl2", LinuxDeviceEvidence{})
	if !got.AccelReachable {
		t.Fatalf("windows CUDA under WSL2: AccelReachable = false, want true (WSL2 CUDA passthrough)")
	}
	if topo := SuggestedTopology(got); topo != "in_container" {
		t.Fatalf("windows CUDA under WSL2: topology = %q, want in_container", topo)
	}
}

// Row 3 — Linux shares the device namespace, so the node decides.
func TestLinuxNPUWithAccelNodeIsInContainer(t *testing.T) {
	got := resolve(HardwareInfo{OS: "linux", Arch: "amd64", IntelNPU: true}, "docker-linux", LinuxDeviceEvidence{AccelNode: true})
	if !got.AccelReachable {
		t.Fatalf("linux IntelNPU with /dev/accel/accel0: AccelReachable = false, want true")
	}
	if topo := SuggestedTopology(got); topo != "in_container" {
		t.Fatalf("linux IntelNPU with /dev/accel/accel0: topology = %q, want in_container", topo)
	}
}

// Row 4 — same hardware, no node: the container cannot see it.
func TestLinuxNPUWithoutAccelNodeIsHostSidecar(t *testing.T) {
	got := resolve(HardwareInfo{OS: "linux", Arch: "amd64", IntelNPU: true}, "docker-linux", LinuxDeviceEvidence{})
	if got.AccelReachable {
		t.Fatalf("linux IntelNPU with no /dev/accel: AccelReachable = true, want false")
	}
	if topo := SuggestedTopology(got); topo != "host_sidecar" {
		t.Fatalf("linux IntelNPU with no /dev/accel: topology = %q, want host_sidecar", topo)
	}
}

// Row 5a — ROCm needs BOTH nodes.
func TestLinuxROCmWithKFDAndDRIIsInContainer(t *testing.T) {
	got := resolve(HardwareInfo{OS: "linux", Arch: "amd64", ROCm: true}, "docker-linux", LinuxDeviceEvidence{KFD: true, DRI: true})
	if !got.AccelReachable {
		t.Fatalf("linux ROCm with /dev/kfd and /dev/dri: AccelReachable = false, want true")
	}
	if topo := SuggestedTopology(got); topo != "in_container" {
		t.Fatalf("linux ROCm with /dev/kfd and /dev/dri: topology = %q, want in_container", topo)
	}
}

// Row 5b — /dev/kfd alone is not enough; a lazy OR would pass 5a and fail here.
func TestLinuxROCmWithKFDOnlyIsHostSidecar(t *testing.T) {
	got := resolve(HardwareInfo{OS: "linux", Arch: "amd64", ROCm: true}, "docker-linux", LinuxDeviceEvidence{KFD: true})
	if got.AccelReachable {
		t.Fatalf("linux ROCm with /dev/kfd only: AccelReachable = true, want false (/dev/dri missing)")
	}
	if topo := SuggestedTopology(got); topo != "host_sidecar" {
		t.Fatalf("linux ROCm with /dev/kfd only: topology = %q, want host_sidecar", topo)
	}
}

// Row 6 — a present GPU the toolkit cannot hand to a container.
func TestLinuxCUDAWithoutNvidiaContainerRuntimeIsHostSidecar(t *testing.T) {
	got := resolve(HardwareInfo{OS: "linux", Arch: "amd64", CUDA: true}, "docker-linux", LinuxDeviceEvidence{DRI: true})
	if got.AccelReachable {
		t.Fatalf("linux CUDA with no nvidia-container-runtime: AccelReachable = true, want false")
	}
	if topo := SuggestedTopology(got); topo != "host_sidecar" {
		t.Fatalf("linux CUDA with no nvidia-container-runtime: topology = %q, want host_sidecar", topo)
	}
}

// Row 7 — nothing to lose on CPU.
func TestLinuxNoAcceleratorIsInContainer(t *testing.T) {
	got := resolve(HardwareInfo{OS: "linux", Arch: "amd64"}, "docker-linux", LinuxDeviceEvidence{})
	if !got.AccelReachable {
		t.Fatalf("linux CPU-only: AccelReachable = false, want true (CPU is always reachable)")
	}
	if topo := SuggestedTopology(got); topo != "in_container" {
		t.Fatalf("linux CPU-only: topology = %q, want in_container", topo)
	}
}

// Row 8 — Docker Desktop on macOS is a VM: no Metal passthrough.
func TestDarwinMPSIsHostSidecar(t *testing.T) {
	got := resolve(HardwareInfo{OS: "darwin", Arch: "arm64", MPS: true}, "docker-desktop-macos", LinuxDeviceEvidence{})
	if got.AccelReachable {
		t.Fatalf("darwin MPS under docker-desktop-macos: AccelReachable = true, want false")
	}
	if topo := SuggestedTopology(got); topo != "host_sidecar" {
		t.Fatalf("darwin MPS under docker-desktop-macos: topology = %q, want host_sidecar", topo)
	}
}

// Row 9 — macOS on CPU still containerises fine.
func TestDarwinNoAcceleratorIsInContainer(t *testing.T) {
	got := resolve(HardwareInfo{OS: "darwin", Arch: "arm64"}, "docker-desktop-macos", LinuxDeviceEvidence{})
	if !got.AccelReachable {
		t.Fatalf("darwin CPU-only: AccelReachable = false, want true")
	}
	if topo := SuggestedTopology(got); topo != "in_container" {
		t.Fatalf("darwin CPU-only: topology = %q, want in_container", topo)
	}
}

// Row 10 — the contrast that is the point: the SAME hardware as row 1, not
// containerised, reaches its device directly.
func TestWindowsNPUNotContainerisedIsInProcess(t *testing.T) {
	got := resolve(HardwareInfo{OS: "windows", Arch: "amd64", IntelNPU: true}, "", LinuxDeviceEvidence{})
	if !got.AccelReachable {
		t.Fatalf("windows IntelNPU, no container runtime: AccelReachable = false, want true")
	}
	if topo := SuggestedTopology(got); topo != "in_process" {
		t.Fatalf("windows IntelNPU, no container runtime: topology = %q, want in_process", topo)
	}
}

// Row 11 — matchContainerRuntime, one assert per OS plus the no-Docker case.
func TestMatchContainerRuntimeWindows(t *testing.T) {
	if got := matchContainerRuntime("windows", true); got != "docker-desktop-wsl2" {
		t.Fatalf("matchContainerRuntime(windows, true) = %q, want docker-desktop-wsl2", got)
	}
}

func TestMatchContainerRuntimeDarwin(t *testing.T) {
	if got := matchContainerRuntime("darwin", true); got != "docker-desktop-macos" {
		t.Fatalf("matchContainerRuntime(darwin, true) = %q, want docker-desktop-macos", got)
	}
}

func TestMatchContainerRuntimeLinux(t *testing.T) {
	if got := matchContainerRuntime("linux", true); got != "docker-linux" {
		t.Fatalf("matchContainerRuntime(linux, true) = %q, want docker-linux", got)
	}
}

func TestMatchContainerRuntimeNoDocker(t *testing.T) {
	if got := matchContainerRuntime("linux", false); got != "" {
		t.Fatalf("matchContainerRuntime(linux, false) = %q, want \"\" (no Docker evidence)", got)
	}
}
