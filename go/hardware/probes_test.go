package hardware

import "testing"

// Fixture tests for the pure matcher functions (Wave H1 / D41).
// Each row is asserted individually; no grouped asserts.

// --- Windows NPU matcher ---

func TestMatchIntelNPUWindowsInputDevicesOnlyIsFalse(t *testing.T) {
	names := []string{
		"USB Input Device",
		"Microsoft Input Configuration Device",
		"HID Keyboard Input",
		"Intel(R) Smart Sound Technology",
	}
	if matchIntelNPUWindows(names) {
		t.Fatalf("matchIntelNPUWindows(%q) = true, want false: no NPU present, 'Input' must not match", names)
	}
}

func TestMatchIntelNPUWindowsAIBoostIsTrue(t *testing.T) {
	names := []string{
		"USB Input Device",
		"Microsoft Input Configuration Device",
		"HID Keyboard Input",
		"Intel(R) Smart Sound Technology",
		"Intel(R) AI Boost",
	}
	if !matchIntelNPUWindows(names) {
		t.Fatalf("matchIntelNPUWindows(%q) = false, want true: 'Intel(R) AI Boost' is the NPU", names)
	}
}

func TestMatchIntelNPUWindowsNPUWordIsTrue(t *testing.T) {
	names := []string{"Intel(R) NPU"}
	if !matchIntelNPUWindows(names) {
		t.Fatalf("matchIntelNPUWindows(%q) = false, want true: word-boundary 'npu' must match", names)
	}
}

func TestMatchIntelNPUWindowsQualcommHexagonNPUIsFalse(t *testing.T) {
	names := []string{"Qualcomm(R) Hexagon(TM) NPU"}
	if matchIntelNPUWindows(names) {
		t.Fatalf("matchIntelNPUWindows(%q) = true, want false: a non-Intel NPU must not select the Intel path", names)
	}
}

// --- Linux NPU matcher ---

func TestMatchIntelNPULinuxInputDeviceControllerIsFalse(t *testing.T) {
	lspci := "00:14.0 USB controller [0c03]: Intel Corporation Meteor Lake USB [8086:7e7d]\n" +
		"00:15.0 Input device controller [0900]: Generic Vendor Widget [1234:5678]\n"
	if matchIntelNPULinux(lspci) {
		t.Fatalf("matchIntelNPULinux(%q) = true, want false: 'Input device controller' must not match as npu", lspci)
	}
}

func TestMatchIntelNPULinuxMeteorLakeNPUIsTrue(t *testing.T) {
	lspci := "00:0b.0 Processing accelerators [1200]: Intel Corporation Meteor Lake NPU [8086:7d1d]\n"
	if !matchIntelNPULinux(lspci) {
		t.Fatalf("matchIntelNPULinux(%q) = false, want true: Meteor Lake NPU line present", lspci)
	}
}

func TestMatchIntelNPULinuxIntelAndVPUOnDifferentLinesIsFalse(t *testing.T) {
	lspci := "00:02.0 VGA compatible controller [0300]: Intel Corporation Graphics [8086:7d55]\n" +
		"00:0b.0 Multimedia controller [0480]: Other Vendor VPU [abcd:ef01]\n"
	if matchIntelNPULinux(lspci) {
		t.Fatalf("matchIntelNPULinux(%q) = true, want false: 'intel' and 'vpu' are on different lines", lspci)
	}
}

// --- ROCm matcher (D44) ---

func TestMatchROCmOutputSMIVersionBannerIsFalse(t *testing.T) {
	out := "ROCM-SMI version: 1.4.1\nROCM-SMI-LIB version: 7.0.0\n"
	if matchROCmOutput(out) {
		t.Fatalf("matchROCmOutput(%q) = true, want false: a --version banner is not device evidence (D44)", out)
	}
}

func TestMatchROCmOutputHipconfigVersionBannerIsFalse(t *testing.T) {
	out := "6.0.32830-d62f6a171\n"
	if matchROCmOutput(out) {
		t.Fatalf("matchROCmOutput(%q) = true, want false: a hipconfig --version banner is not device evidence (D44)", out)
	}
}

func TestMatchROCmOutputRocminfoGPUAgentIsTrue(t *testing.T) {
	out := "*******\nAgent 2\n*******\n  Name:                    gfx90a\n" +
		"  Marketing Name:          AMD Instinct MI210\n  Device Type:             GPU\n"
	if !matchROCmOutput(out) {
		t.Fatalf("matchROCmOutput(%q) = false, want true: rocminfo GPU agent block with a gfx90a ISA line", out)
	}
}

func TestMatchROCmOutputSMIGPUTableRowIsTrue(t *testing.T) {
	out := "GPU  Temp   AvgPwr  SCLK    MCLK    Fan    Perf  PwrCap  VRAM%  GPU%\n" +
		"GPU[0]\t\t: Temperature (Sensor edge) (C): 41.0\n"
	if !matchROCmOutput(out) {
		t.Fatalf("matchROCmOutput(%q) = false, want true: rocm-smi GPU row with a temperature column", out)
	}
}

func TestMatchROCmOutputEmptyIsFalse(t *testing.T) {
	if matchROCmOutput("") {
		t.Fatal(`matchROCmOutput("") = true, want false: no output is not device evidence`)
	}
}

func TestMatchROCmOutputAMDWithoutDeviceEvidenceIsFalse(t *testing.T) {
	out := "AMD ROCm HIP compiler\n"
	if matchROCmOutput(out) {
		t.Fatalf("matchROCmOutput(%q) = true, want false: 'AMD' with no device name is not device evidence", out)
	}
}

// --- XPU matchers ---

func TestMatchIntelXPUOutputOpenCLCPUOnlyIsFalse(t *testing.T) {
	out := "[opencl:cpu:0] Intel(R) Core(TM) Ultra 7 258V\n"
	if matchIntelXPUOutput(out) {
		t.Fatalf("matchIntelXPUOutput(%q) = true, want false: CPU-only sycl-ls output is not device evidence", out)
	}
}

func TestMatchIntelXPUOutputLevelZeroGPUIsTrue(t *testing.T) {
	out := "[level_zero:gpu:0] Intel(R) Arc(TM) A770 Graphics\n"
	if !matchIntelXPUOutput(out) {
		t.Fatalf("matchIntelXPUOutput(%q) = false, want true: level_zero:gpu Intel Arc line present", out)
	}
}

func TestMatchIntelXPUWindowsDisplayCrossNameIsFalse(t *testing.T) {
	names := []string{"Intel(R) UHD Graphics", "NVIDIA GeForce Arc-lamp Whatever"}
	if matchIntelXPUWindowsDisplay(names) {
		t.Fatalf("matchIntelXPUWindowsDisplay(%q) = true, want false: 'intel' and 'arc' must not cross-match between names", names)
	}
}
