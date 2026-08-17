package hardware

import (
	"bufio"
	"context"
	"os"
	"runtime"
	"strings"

	"github.com/nce/tri-stack/internal/executil"
)

func cpuModel(ctx context.Context) string {
	switch runtime.GOOS {
	case "linux":
		return cpuModelLinux()
	case "darwin":
		out, err := executil.Output(ctx, "sysctl", "-n", "machdep.cpu.brand_string")
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(out))
	case "windows":
		out, err := executil.Output(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command",
			"(Get-CimInstance Win32_Processor).Name")
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(out))
	default:
		return ""
	}
}

func cpuModelLinux() string {
	f, err := os.Open("/proc/cpuinfo")
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if strings.HasPrefix(line, "model name") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 {
				return strings.TrimSpace(parts[1])
			}
		}
		// aarch64 often uses different key
		if strings.HasPrefix(line, "Model") && strings.Contains(line, ":") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 {
				return strings.TrimSpace(parts[1])
			}
		}
	}
	return ""
}
