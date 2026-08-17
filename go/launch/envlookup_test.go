package launch

import "testing"

func TestUpsertEnv(t *testing.T) {
	base := []string{"FOO=1", "NCE_BACKEND=cpu", "BAR=2"}
	got := UpsertEnv(base, "NCE_BACKEND", "cuda")
	if LookupEnv(got, "NCE_BACKEND") != "cuda" {
		t.Fatalf("backend: %v", got)
	}
	if LookupEnv(got, "FOO") != "1" || LookupEnv(got, "BAR") != "2" {
		t.Fatalf("preserved: %v", got)
	}
	got2 := UpsertEnv([]string{"A=b"}, "NCE_BACKEND", "mps")
	if LookupEnv(got2, "NCE_BACKEND") != "mps" {
		t.Fatal(got2)
	}
}
