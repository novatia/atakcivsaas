#!/usr/bin/env bash
# Genera il data package ATAK PCN-Geoportale-Italia.zip
set -euo pipefail
cd "$(dirname "$0")"

OUT="PCN-Geoportale-Italia.zip"
rm -f "$OUT"

if command -v zip >/dev/null 2>&1; then
    zip -X "$OUT" MANIFEST/manifest.xml PCN_*.xml
else
    # Fallback Windows: usa .NET per garantire separatori "/" nelle entry
    powershell -NoProfile -Command '
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $out = Join-Path (Get-Location) "PCN-Geoportale-Italia.zip"
        $zip = [System.IO.Compression.ZipFile]::Open($out, "Create")
        try {
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "MANIFEST\manifest.xml", "MANIFEST/manifest.xml")
            Get-ChildItem -Filter "PCN_*.xml" | ForEach-Object {
                [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $_.Name)
            }
        } finally { $zip.Dispose() }
    '
fi
echo "Creato: $OUT"
