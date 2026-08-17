//go:build darwin

package notify

import (
	"bytes"
	"os/exec"
	"strings"
)

func platformNotifier() UserNotifier {
	return darwinNotifier{}
}

type darwinNotifier struct{}

func escapeAppleScriptLiteral(s string) string {
	s = strings.ReplaceAll(s, "\r\n", "\n")
	s = strings.ReplaceAll(s, "\n", " ")
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `"`, `\"`)
	return s
}

// Error runs AppleScript `display alert` (NSAlert-equivalent UX without CGO).
func (darwinNotifier) Error(title, message string) {
	script := `display alert "` + escapeAppleScriptLiteral(title) + `" message "` + escapeAppleScriptLiteral(message) + `" as critical buttons {"OK"}`
	cmd := exec.Command("/usr/bin/osascript", "-e", script)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	_ = cmd.Run()
	if stderr.Len() > 0 {
		LogNotifier{}.Error(title, message)
	}
}
