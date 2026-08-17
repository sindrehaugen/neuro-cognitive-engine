package hardware

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/nce/tri-stack/internal/executil"
)

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
		var err error
		if len(t.args) == 0 {
			err = executil.Run(c2, t.name)
		} else {
			err = executil.Run(c2, t.name, t.args...)
		}
		cancel()
		if err == nil {
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
			var err error
			if len(t.args) == 0 {
				err = executil.Run(c2, p)
			} else {
				err = executil.Run(c2, p, t.args...)
			}
			cancel()
			if err == nil {
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
	lower := strings.ToLower(string(out))
	return strings.Contains(lower, "npu") ||
		strings.Contains(lower, "intel corporation meteor lake-npu") ||
		strings.Contains(lower, "vpu") && strings.Contains(lower, "intel") ||
		strings.Contains(lower, "ai boost") ||
		strings.Contains(lower, "intel npu")
}

func probeIntelNPUWindows(ctx context.Context) bool {
	// No raw device names logged; only match heuristic.
	const ps = `Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | ForEach-Object { $_.FriendlyName }`
	out, err := executil.Output(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	if err != nil {
		return false
	}
	lower := strings.ToLower(string(out))
	return strings.Contains(lower, "npu") ||
		strings.Contains(lower, "neural") ||
		strings.Contains(lower, "ai boost")
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
		var err error
		if len(t.args) == 0 {
			err = executil.Run(c2, t.name)
		} else {
			err = executil.Run(c2, t.name, t.args...)
		}
		cancel()
		if err == nil {
			return true
		}
	}
	switch runtime.GOOS {
	case "linux":
		out, err := executil.Output(ctx, "lspci", "-nn")
		if err != nil {
			return false
		}
		lower := strings.ToLower(string(out))
		if strings.Contains(lower, "intel") && strings.Contains(lower, "arc") {
			return true
		}
		if strings.Contains(lower, "data center gpu max") || strings.Contains(lower, "flex series") {
			return true
		}
		return false
	case "windows":
		const ps = `Get-PnpDevice -PresentOnly -Class Display -ErrorAction SilentlyContinue | ForEach-Object { $_.FriendlyName }`
		out, err := executil.Output(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
		if err != nil {
			return false
		}
		lower := strings.ToLower(string(out))
		return strings.Contains(lower, "intel") && (strings.Contains(lower, "arc") || strings.Contains(lower, "data center"))
	default:
		return false
	}
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
