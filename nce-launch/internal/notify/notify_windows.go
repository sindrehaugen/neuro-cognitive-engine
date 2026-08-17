//go:build windows

package notify

import (
	"syscall"
	"unsafe"
)

var (
	user32           = syscall.NewLazyDLL("user32.dll")
	procMessageBoxW  = user32.NewProc("MessageBoxW")
)

const (
	mbOK           = 0x0
	mbIconError    = 0x10
	mbSystemModal  = 0x1000
	mbTopmost      = 0x40000
)

func platformNotifier() UserNotifier {
	return windowsNotifier{}
}

type windowsNotifier struct{}

func (windowsNotifier) Error(title, message string) {
	utf16Message, err := syscall.UTF16PtrFromString(message)
	if err != nil {
		utf16Message, _ = syscall.UTF16PtrFromString("invalid error message encoding")
	}
	utf16Title, err := syscall.UTF16PtrFromString(title)
	if err != nil {
		utf16Title, _ = syscall.UTF16PtrFromString("NCE")
	}
	flags := uintptr(mbOK | mbIconError | mbSystemModal | mbTopmost)
	procMessageBoxW.Call(0,
		uintptr(unsafe.Pointer(utf16Message)),
		uintptr(unsafe.Pointer(utf16Title)),
		flags,
	)
}
