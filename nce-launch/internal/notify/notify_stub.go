//go:build !windows && !darwin

package notify

func platformNotifier() UserNotifier {
	return LogNotifier{}
}
