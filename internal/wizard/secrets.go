package wizard

// secrets.go generates the strong defaults the wizard offers for
// SERVICE_TOKEN and ADMIN_PASSWORD. Randomness is injectable so tests are
// deterministic; production uses crypto/rand.

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
)

// randomHex returns 2n hexadecimal characters from rand. A nil rand falls
// back to crypto/rand.Reader.
func randomHex(randSource io.Reader, n int) (string, error) {
	if randSource == nil {
		randSource = rand.Reader
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(randSource, buf); err != nil {
		return "", fmt.Errorf("generating secret: %w", err)
	}
	return hex.EncodeToString(buf), nil
}
