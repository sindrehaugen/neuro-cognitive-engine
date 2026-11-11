#!/bin/bash
set -e
#
# Prerequisites: Universal binary at build/macos/trimcp-launch (built in CI via
# go/cmd/trimcp-launch: lipo amd64+arm64). That binary is copied as CFBundleExecutable.
#

APP_NAME="TriMCP"
APP_DIR="build/macos/${APP_NAME}.app"
DMG_NAME="${APP_NAME}-universal.dmg"
DMG_PATH="build/macos/${DMG_NAME}"
SIGNING_IDENTITY="${APPLE_DEV_ID}" # e.g., "Developer ID Application: Your Company (TEAMID)"

echo "==> Scaffolding .app bundle structure"
mkdir -p "${APP_DIR}/Contents/MacOS"
mkdir -p "${APP_DIR}/Contents/Resources"
mkdir -p "${APP_DIR}/Contents/Frameworks"

# Copy Go shim as the main executable
cp build/macos/trimcp-launch "${APP_DIR}/Contents/MacOS/${APP_NAME}"

# Copy Python framework and assets
mkdir -p "${APP_DIR}/Contents/Resources/app"
cp -R build/macos/assets/python/* "${APP_DIR}/Contents/Frameworks/"
cp -R build/macos/assets/models "${APP_DIR}/Contents/Resources/"
cp -R build/macos/assets/wheels "${APP_DIR}/Contents/Resources/"

# Copy TriMCP v1.0 deployment stack
cp docker-compose.yml "${APP_DIR}/Contents/Resources/app/"
cp Caddyfile "${APP_DIR}/Contents/Resources/app/"
cp requirements.txt "${APP_DIR}/Contents/Resources/app/"
cp -R trimcp "${APP_DIR}/Contents/Resources/app/"
cp -R admin "${APP_DIR}/Contents/Resources/app/"
cp -R deploy "${APP_DIR}/Contents/Resources/app/"
cp *.py "${APP_DIR}/Contents/Resources/app/"

# Copy IDE patching script
cp build/macos/Patch-IDEConfig.sh "${APP_DIR}/Contents/Resources/"
chmod +x "${APP_DIR}/Contents/Resources/Patch-IDEConfig.sh"

# Create Info.plist
cat > "${APP_DIR}/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>com.company.trimcp</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
</dict>
</plist>
EOF

echo "==> Checking for signing credentials"
if [ -n "${APPLE_DEV_ID}" ] && [ -n "${APPLE_DEV_PASS}" ] && [ -n "${APPLE_TEAM_ID}" ]; then
    echo "==> Codesigning .app bundle"
    # Sign inner frameworks and libraries first
    find "${APP_DIR}/Contents/Frameworks" -type f \( -name "*.dylib" -o -name "*.so" \) -exec codesign --force --sign "${SIGNING_IDENTITY}" --options runtime {} \;
    # Sign the main app
    codesign --force --deep --sign "${SIGNING_IDENTITY}" --options runtime "${APP_DIR}"
else
    echo "==> Skipping codesigning (No APPLE_DEV_ID provided)"
fi

echo "==> Creating DMG"
# Assuming create-dmg is installed via Homebrew (brew install create-dmg)
# Fallback to hdiutil if create-dmg is not available
if command -v create-dmg &> /dev/null; then
    create-dmg \
      --volname "${APP_NAME} Installer" \
      --window-pos 200 120 \
      --window-size 600 400 \
      --icon-size 100 \
      --icon "${APP_NAME}.app" 150 190 \
      --hide-extension "${APP_NAME}.app" \
      --app-drop-link 450 190 \
      "${DMG_PATH}" \
      "${APP_DIR}"
else
    echo "create-dmg not found, using hdiutil"
    hdiutil create -volname "${APP_NAME} Installer" -srcfolder "${APP_DIR}" -ov -format UDZO "${DMG_PATH}"
fi

if [ -n "${APPLE_DEV_ID}" ] && [ -n "${APPLE_DEV_PASS}" ] && [ -n "${APPLE_TEAM_ID}" ]; then
    echo "==> Codesigning DMG"
    codesign --force --sign "${SIGNING_IDENTITY}" "${DMG_PATH}"

    echo "==> Notarizing DMG"
    xcrun notarytool submit "${DMG_PATH}" \
      --apple-id "${APPLE_DEV_ID}" \
      --password "${APPLE_DEV_PASS}" \
      --team-id "${APPLE_TEAM_ID}" \
      --wait

    echo "==> Stapling Notarization Ticket"
    xcrun stapler staple "${DMG_PATH}"
else
    echo "==> Skipping DMG codesigning and notarization (No credentials provided)"
fi

echo "==> macOS Build Complete: ${DMG_PATH}"
