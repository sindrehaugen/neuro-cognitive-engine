// Package hardware implements installer-time host detection for NCE (§8.4).
// All subprocess probes use bounded contexts (<5s) so nce-launch cannot hang on boot.
package hardware

import (
	"context"
	"runtime"
	"sync"
	"time"
)

// Budget for the full detectHardware snapshot (wall clock). Individual probes use probeTimeout.
const (
	overallBudget = 5 * time.Second
	probeTimeout  = 4 * time.Second
)

// HardwareInfo mirrors §8.4 (detectHardware / HardwareInfo) and maps to NCE_BACKEND in .env.
// CPUModel is the §8.4 detectCPU() string.
type HardwareInfo struct {
	CUDA     bool   `json:"cuda"`      // NVIDIA — nvidia-smi
	ROCm     bool   `json:"rocm"`      // AMD ROCm stack
	IntelNPU bool   `json:"intel_npu"` // Intel OpenVINO / Meteor Lake class NPU
	IntelXPU bool   `json:"intel_xpu"` // Intel discrete GPU (Arc, Data Center)
	MPS      bool   `json:"mps"`       // Apple Silicon
	CPUModel string `json:"cpu_model,omitempty"`
	OS       string `json:"os"`
	Arch     string `json:"arch"`

	// D43: presence is not reachability. A device can be healthy on the host and
	// invisible to the runtime that needs it (Intel NPU under WSL2, Metal under a
	// Docker Desktop VM), so the snapshot records which runtime would consume it
	// and whether the chosen accelerator is visible there.
	ContainerRuntime string `json:"container_runtime,omitempty"` // "", "docker-linux", "docker-desktop-wsl2", "docker-desktop-macos"
	AccelReachable   bool   `json:"accel_reachable"`             // is the SuggestedBackend accelerator visible to that runtime?
}

// DetectHardware implements §8.4 host probing (installer pseudocode name: detectHardware).
// Probes run in parallel under a 5s wall budget; each subprocess uses a 4s cap so missing nvidia-smi,
// blocked drivers, or stuck tools cannot hang the shim (CommandContext terminates the process on cancel).
func DetectHardware() HardwareInfo {
	parent, cancel := context.WithTimeout(context.Background(), overallBudget)
	defer cancel()

	var mu sync.Mutex
	info := HardwareInfo{OS: runtime.GOOS, Arch: runtime.GOARCH}

	var wg sync.WaitGroup
	one := func(fn func(context.Context)) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, done := context.WithTimeout(parent, probeTimeout)
			defer done()
			fn(c)
		}()
	}

	// Apple Silicon MPS — native arm64 or amd64-under-Rosetta on Apple hardware (hw.optional.arm64).
	one(func(c context.Context) {
		if hostAppleSiliconMPS(c) {
			mu.Lock()
			info.MPS = true
			mu.Unlock()
		}
	})

	one(func(c context.Context) {
		if probeNVIDIA(c) {
			mu.Lock()
			info.CUDA = true
			mu.Unlock()
		}
	})
	one(func(c context.Context) {
		if probeROCm(c) {
			mu.Lock()
			info.ROCm = true
			mu.Unlock()
		}
	})
	one(func(c context.Context) {
		if probeIntelNPU(c) {
			mu.Lock()
			info.IntelNPU = true
			mu.Unlock()
		}
	})
	one(func(c context.Context) {
		if probeIntelXPU(c) {
			mu.Lock()
			info.IntelXPU = true
			mu.Unlock()
		}
	})
	one(func(c context.Context) {
		m := cpuModel(c)
		if m != "" {
			mu.Lock()
			info.CPUModel = m
			mu.Unlock()
		}
	})

	wg.Wait()

	// Reachability is a function of the probe results (which accelerator would be
	// chosen), so it is computed from info after the wait-group completes rather
	// than raced alongside the probes. Neither call spawns a subprocess, so the
	// 5s budget above is unaffected.
	info.ContainerRuntime = detectContainerRuntime()
	info.AccelReachable = accelReachable(info, info.ContainerRuntime, gatherLinuxDeviceEvidence())
	return info
}

// SuggestedBackend matches nce.embeddings.detect_backend priority (CUDA → ROCm → XPU → OpenVINO NPU → MPS → CPU).
func SuggestedBackend(h HardwareInfo) string {
	switch {
	case h.CUDA:
		return "cuda"
	case h.ROCm:
		return "rocm"
	case h.IntelXPU:
		return "xpu"
	case h.IntelNPU:
		return "openvino_npu"
	case h.MPS:
		return "mps"
	default:
		return "cpu"
	}
}

// SuggestedTopology reports where the embedding model should run, given what the
// host has and what the container runtime can actually reach (D43). It returns
// exactly one of "in_process", "in_container", "host_sidecar".
//
// Detection only: nothing here writes an env var, changes a deployment, or starts
// a sidecar — the value is emitted and logged for a later wave to consume.
func SuggestedTopology(h HardwareInfo) string {
	if h.ContainerRuntime == "" {
		// Not containerised: the process reaches the host device directly.
		return "in_process"
	}
	if SuggestedBackend(h) == "cpu" {
		// No accelerator to lose; CPU is reachable everywhere.
		return "in_container"
	}
	if h.AccelReachable {
		return "in_container"
	}
	// Host-visible but container-invisible (the D43 case): run it host-native.
	return "host_sidecar"
}
