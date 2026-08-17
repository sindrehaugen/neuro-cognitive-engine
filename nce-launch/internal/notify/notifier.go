package notify

// UserNotifier shows platform-native (or best-effort) user-facing errors.
type UserNotifier interface {
	Error(title, message string)
}

// LogNotifier writes errors to stderr only (fallback / CI).
type LogNotifier struct{}

func (LogNotifier) Error(title, message string) {
	println(title + ": " + message)
}

// New picks the platform implementation (MessageBoxW on Windows, osascript alert on macOS).
func New() UserNotifier {
	return platformNotifier()
}
