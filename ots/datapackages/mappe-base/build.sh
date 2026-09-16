#!/usr/bin/env bash
# Genera il data package ATAK Mappe-Base.zip
set -euo pipefail
cd "$(dirname "$0")"

OUT="Mappe-Base.zip"
rm -f "$OUT"

if command -v zip >/dev/null 2>&1; then
    zip -X "$OUT" MANIFEST/manifest.xml *.xml
else
    # Fallback Windows: usa .NET per garantire separatori "/" nelle entry
    powershell -NoProfile -Command '
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $out = Join-Path (Get-Location) "Mappe-Base.zip"
        $zip = [System.IO.Compression.ZipFile]::Open($out, "Create")
        try {
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "MANIFEST\manifest.xml", "MANIFEST/manifest.xml")
            Get-ChildItem -Filter "*.xml" | ForEach-Object {
                [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $_.Name)
            }
        } finally { $zip.Dispose() }
    '
fi
echo "Creato: $OUT"
