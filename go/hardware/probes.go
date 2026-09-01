package hardware

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"

	"github.com/nce/tri-stack/internal/executil"
)

// LinuxDeviceEvidence is the host-device evidence the reachability rules need on
// Linux, where the host and the container share a device namespace. It exists so
// accelReachable takes no filesystem dependency and the whole rule matrix is
// testable with no hardware — it is a seam, not an abstraction layer.
type LinuxDeviceEvidence struct {
	KFD                    bool // /dev/kfd — ROCm compute device
	DRI                    bool // /dev/dri — render nodes (ROCm, Intel XPU)
	AccelNode              bool // a /dev/accel/accel* node — Intel NPU
	NvidiaContainerRuntime bool // nvidia-container-runtime on PATH — nvidia-container-toolkit installed
}

// gatherLinuxDeviceEvidence is the thin probe half: no decisions, just evidence.
// It stats device nodes only, so it adds no wall clock to the DetectHardware budget.
func gatherLinuxDeviceEvidence() LinuxDeviceEvidence {
	if runtime.GOOS != "linux" {
		return LinuxDeviceEvidence{}
	}
	var e LinuxDeviceEvidence
	if _, err := os.Stat("/dev/kfd"); err == nil {
		e.KFD = true
	}
	if _, err := os.Stat("/dev/dri"); err == nil {
		e.DRI = true
	}
	if m, err := filepath.Glob("/dev/accel/accel*"); err == nil && len(m) > 0 {
		e.AccelNode = true
	}
	if _, err := exec.LookPath("nvidia-container-runtime"); err == nil {
		e.NvidiaContainerRuntime = true
	}
	return e
}

// detectContainerRuntime is the thin probe half for the runtime that would consume
// the accelerator. Docker evidence is LookPath only — no `docker info` call, which
// would spend seconds of the 5s budget talking to a daemon.
func detectContainerRuntime() string {
	_, err := exec.LookPath("docker")
	return matchContainerRuntime(runtime.GOOS, err == nil)
}

// matchContainerRuntime is the pure matcher: which container runtime would run the
// stack on this OS. "" means no Docker evidence at all, i.e. not containerised.
func matchContainerRuntime(os string, dockerPresent bool) string {
	if !dockerPresent {
		return ""
	}
	switch os {
	case "windows":
		return "docker-desktop-wsl2"
	case "darwin":
		return "docker-desktop-macos"
	case "linux":
		return "docker-linux"
	default:
		return ""
	}
}

// accelReachable reports whether the accelerator SuggestedBackend(h) would pick is
// visible to the given container runtime — reachability of the *chosen* device, not
// of any device. DetectHardware runs on the host, never inside a container, so this
// is inferred from (host OS, runtime, accelerator kind) plus Linux device nodes; it
// never probes from inside a container.
func accelReachable(h HardwareInfo, runtime string, linuxDevs LinuxDeviceEvidence) bool {
	if runtime == "" {
		// Not containerised: in-process reaches the host device directly.
		return true
	}
	backend := SuggestedBackend(h)
	if backend == "cpu" {
		// No accelerator was chosen; the CPU is always reachable.
		return true
	}
	switch h.OS {
	case "linux":
		switch backend {
		case "cuda":
			return linuxDevs.NvidiaContainerRuntime
		case "rocm":
			// Both nodes are required: /dev/kfd alone cannot render or submit queues.
			return linuxDevs.KFD && linuxDevs.DRI
		case "openvino_npu":
			return linuxDevs.AccelNode
		case "xpu":
			return linuxDevs.DRI
		}
		return false
	case "windows":
		// WSL2 passes CUDA through; it exposes no /dev/accel and no /dev/dri, so the
		// Intel NPU and Intel XPU are host-visible but container-invisible (D43).
		return backend == "cuda"
	case "darwin":
		// Docker Desktop is a VM with no Metal passthrough, so MPS never reaches it.
		return false
	default:
		return false
	}
}

func probeNVIDIA(ctx context.Context) bool {
	// -L lists GPUs; missing binary or driver hang -> ctx cancels.
	out, err := executil.Output(ctx, "nvidia-smi", "-L")
	if err != nil {
		return false
	}
	s := strings.ToLower(string(bytes.TrimSpace(out)))
	return strings.Contains(s, "gpu")
}

func probeROCm(ctx context.Context) bool {
	tests := []struct {
		name string
		args []string
	}{
		{"rocm-smi", []string{"--version"}},
		{"hipconfig", []string{"--version"}},
		// §8.4 AMD / ROCm stack — rocminfo is present on many installs where smi is not in PATH.
		{"rocminfo", nil},
	}
	for _, t := range tests {
		if ctx.Err() != nil {
			return false
		}
		// Shorter per-attempt slice so we do not exhaust the parent probe budget on sequential misses.
		c2, cancel := context.WithTimeout(ctx, 2*time.Second)
		var out []byte
		var err error
		if len(t.args) == 0 {
			out, err = executil.Output(c2, t.name)
		} else {
			out, err = executil.Output(c2, t.name, t.args...)
		}
		cancel()
		// Exit 0 alone is not device evidence: rocm-smi --version and hipconfig
		// --version print a banner and exit 0 on any host with the ROCm packages
		// installed, device or not (D44).
		if err == nil && matchROCmOutput(string(out)) {
			return true
		}
	}
	if runtime.GOOS == "linux" {
		rocmDir := filepath.Join("/opt", "rocm", "bin")
		linuxROCm := []struct {
			name string
			args []string
		}{
			{"rocm-smi", []string{"--version"}},
			{"rocminfo", nil},
		}
		for _, t := range linuxROCm {
			if ctx.Err() != nil {
				return false
			}
			p := filepath.Join(rocmDir, t.name)
			if st, err := os.Stat(p); err != nil || st.IsDir() {
				continue
			}
			c2, cancel := context.WithTimeout(ctx, 2*time.Second)
			var out []byte
			var err error
			if len(t.args) == 0 {
				out, err = executil.Output(c2, p)
			} else {
				out, err = executil.Output(c2, p, t.args...)
			}
			cancel()
			// Same rule as the PATH loop: a /opt/rocm/bin binary that exits 0 is
			// evidence of the ROCm install, not of a device (D44).
			if err == nil && matchROCmOutput(string(out)) {
				return true
			}
		}
	}
	return false
}

func probeIntelNPU(ctx context.Context) bool {
	switch runtime.GOOS {
	case "linux":
		return probeIntelNPULinux(ctx)
	case "windows":
		return probeIntelNPUWindows(ctx)
	case "darwin":
		// No discrete Intel NPU class devices exposed like Windows/Linux today.
		return false
	default:
		return false
	}
}

func probeIntelNPULinux(ctx context.Context) bool {
	out, err := executil.Output(ctx, "lspci", "-nn")
	if err != nil {
		return false
	}
	return matchIntelNPULinux(string(out))
}

func probeIntelNPUWindows(ctx context.Context) bool {
	// Matching is per friendly name: "ai boost" substring or word-boundary "npu",
	// so "Intel(R) NPU" matches but "USB Input Device" does not. Raw device names
	// are still never logged.
	const ps = `Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | ForEach-Object { $_.FriendlyName }`
	out, err := executil.Output(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	if err != nil {
		return false
	}
	return matchIntelNPUWindows(strings.Split(string(out), "\n"))
}

func probeIntelXPU(ctx context.Context) bool {
	tests := []struct {
		name string
		args []string
	}{
		{"xpu-smi", []string{"--version"}},
		{"sycl-ls", nil},
	}
	for _, t := range tests {
		if ctx.Err() != nil {
			return false
		}
		c2, cancel := context.WithTimeout(ctx, 2*time.Second)
		var out []byte
		var err error
		if len(t.args) == 0 {
			out, err = executil.Output(c2, t.name)
		} else {
			out, err = executil.Output(c2, t.name, t.args...)
		}
		cancel()
		// Exit 0 alone is not device evidence (the tool can be installed with no
		// device attached); require an Intel XPU line in the captured output.
		if err == nil && matchIntelXPUOutput(string(out)) {
			return true
		}
	}
	switch runtime.GOOS {
	case "linux":
		out, err := executil.Output(ctx, "lspci", "-nn")
		if err != nil {
			return false
		}
		return matchIntelXPUOutput(string(out))
	case "windows":
		const ps = `Get-PnpDevice -PresentOnly -Class Display -ErrorAction SilentlyContinue | ForEach-Object { $_.FriendlyName }`
		out, err := executil.Output(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
		if err != nil {
			return false
		}
		return matchIntelXPUWindowsDisplay(strings.Split(string(out), "\n"))
	default:
		return false
	}
}

// Word-boundary patterns so device-class words embedded in longer words do not
// match: "USB Input Device" contains the substring "npu" but not the word "npu".
var (
	reWordNPU = regexp.MustCompile(`\bnpu\b`)
	reWordVPU = regexp.MustCompile(`\bvpu\b`)
	reWordArc = regexp.MustCompile(`\barc\b`)
)

// matchIntelNPULinux reports whether a single lspci -nn line identifies an Intel
// NPU: the line must contain "intel" and word "npu", "ai boost", or word "vpu".
// No cross-line matching.
func matchIntelNPULinux(lspciOut string) bool {
	for _, line := range strings.Split(lspciOut, "\n") {
		lower := strings.ToLower(line)
		if !strings.Contains(lower, "intel") {
			continue
		}
		if reWordNPU.MatchString(lower) || strings.Contains(lower, "ai boost") || reWordVPU.MatchString(lower) {
			return true
		}
	}
	return false
}

// matchIntelNPUWindows reports whether any single PnP friendly name identifies
// an Intel NPU: "ai boost" substring, or "intel" plus the word "npu" in the same
// name (a non-Intel NPU, e.g. Qualcomm Hexagon, must not select the Intel path).
func matchIntelNPUWindows(names []string) bool {
	for _, name := range names {
		lower := strings.ToLower(name)
		if strings.Contains(lower, "ai boost") ||
			(strings.Contains(lower, "intel") && reWordNPU.MatchString(lower)) {
			return true
		}
	}
	return false
}

// matchIntelXPUOutput reports whether a single line of xpu-smi, sycl-ls, or
// lspci -nn output identifies an Intel XPU device: the line must contain
// "intel" and word "arc", "data center", "flex", or a level_zero:gpu backend
// entry. Exit status alone is never treated as device evidence.
func matchIntelXPUOutput(out string) bool {
	for _, line := range strings.Split(out, "\n") {
		lower := strings.ToLower(line)
		if !strings.Contains(lower, "intel") {
			continue
		}
		if reWordArc.MatchString(lower) || strings.Contains(lower, "data center") ||
			strings.Contains(lower, "flex") || strings.Contains(lower, "level_zero:gpu") {
			return true
		}
	}
	return false
}

// matchIntelXPUWindowsDisplay reports whether any single Display-class friendly
// name identifies an Intel XPU: "intel" and (word "arc" or "data center") must
// appear in the same name.
func matchIntelXPUWindowsDisplay(names []string) bool {
	for _, name := range names {
		lower := strings.ToLower(name)
		if strings.Contains(lower, "intel") && (reWordArc.MatchString(lower) || strings.Contains(lower, "data center")) {
			return true
		}
	}
	return false
}

// ROCm device-evidence patterns. gfx<digits> is an AMD GPU ISA name (gfx90a,
// gfx1030); an indexed GPU plus a measured column is a rocm-smi device row, which
// a header-only table (no devices) does not produce.
var (
	reROCmGFX        = regexp.MustCompile(`gfx\d`)
	reROCmGPUIndex   = regexp.MustCompile(`gpu\s*\[?\d`)
	reROCmMeasureCol = regexp.MustCompile(`%|\btemp|\b\d+(\.\d+)?\s*c\b`)
)

// matchROCmOutput reports whether a single line of rocm-smi, hipconfig or
// rocminfo output identifies an AMD ROCm *device*. A version banner must never
// match: rocm-smi --version and hipconfig --version print one and exit 0 on any
// host carrying the ROCm/HIP packages, device or not (D44). No cross-line matching.
func matchROCmOutput(out string) bool {
	for _, line := range strings.Split(out, "\n") {
		lower := strings.ToLower(line)
		// rocminfo agent block: a GPU agent line, or an AMD GPU ISA name.
		if (strings.Contains(lower, "agent") && strings.Contains(lower, "gpu")) ||
			reROCmGFX.MatchString(lower) {
			return true
		}
		// rocm-smi GPU table row: an indexed GPU carrying a temperature or
		// utilisation column.
		if reROCmGPUIndex.MatchString(lower) && reROCmMeasureCol.MatchString(lower) {
			return true
		}
		if strings.Contains(lower, "amd") && (strings.Contains(lower, "radeon") ||
			strings.Contains(lower, "instinct") || strings.Contains(lower, "gfx")) {
			return true
		}
	}
	return false
}

// hostAppleSiliconMPS is true when the machine can run PyTorch MPS: native Apple Silicon,
// or amd64-under-Rosetta on Apple hardware (hw.optional.arm64).
func hostAppleSiliconMPS(ctx context.Context) bool {
	if runtime.GOOS != "darwin" {
		return false
	}
	if runtime.GOARCH == "arm64" {
		return true
	}
	if ctx.Err() != nil {
		return false
	}
	out, err := executil.Output(ctx, "sysctl", "-n", "hw.optional.arm64")
	if err != nil {
		return false
	}
	return strings.TrimSpace(string(out)) == "1"
}
