package auth

import (
	"context"
	"os"
	"path/filepath"

	"github.com/99designs/keyring"
	"github.com/AzureAD/microsoft-authentication-library-for-go/apps/cache"
)

const keyringMSALKey = "msal_opaque_cache_v1"

// dualMSALCache persists MSAL's opaque blob to disk and reads a keychain fallback when the file is empty.
// When mirrorToKeychain is true, successful cache exports are also written to the OS credential store.
// Token acquisition flows (silent refresh, device code, etc.) invoke Export after success so the blob stays current.
type dualMSALCache struct {
	path             string
	ring             keyring.Keyring
	mirrorToKeychain bool
}

func newDualMSALCache(path string, mirrorToKeychain bool) *dualMSALCache {
	d := &dualMSALCache{path: path, mirrorToKeychain: mirrorToKeychain}
	kr, err := keyring.Open(keyring.Config{
		ServiceName: "NCE-cloud-msal",
	})
	if err == nil {
		d.ring = kr
	}
	return d
}

func (d *dualMSALCache) Replace(ctx context.Context, c cache.Unmarshaler, _ cache.ReplaceHints) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	data, err := os.ReadFile(d.path)
	if err == nil && len(data) > 0 {
		return c.Unmarshal(data)
	}
	if d.ring == nil {
		return nil
	}
	item, err := d.ring.Get(keyringMSALKey)
	if err != nil || len(item.Data) == 0 {
		return nil
	}
	return c.Unmarshal(item.Data)
}

func (d *dualMSALCache) Export(ctx context.Context, m cache.Marshaler, _ cache.ExportHints) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	data, err := m.Marshal()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(d.path), 0o700); err != nil {
		return err
	}
	tmp := d.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, d.path); err != nil {
		return err
	}
	if d.mirrorToKeychain && d.ring != nil {
		_ = d.ring.Set(keyring.Item{
			Key:   keyringMSALKey,
			Data:  data,
			Label: "NCE MSAL token cache (opaque)",
		})
	}
	return nil
}
