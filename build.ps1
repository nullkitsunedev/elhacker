param(
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Assets = Join-Path $Root "assets"
$LogoPath = Join-Path $Assets "elhacker.png"
$IconPath = Join-Path $Assets "elhacker.ico"
$ModeArgs = @()

if ($OneFile) {
    $ModeArgs += "--onefile"
}

New-Item -ItemType Directory -Force -Path $Assets | Out-Null

Add-Type -AssemblyName System.Drawing

if (Test-Path $LogoPath) {
    $source = [System.Drawing.Image]::FromFile($LogoPath)
    $bitmap = [System.Drawing.Bitmap]::new(256, 256)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.Clear([System.Drawing.Color]::Transparent)

    $scale = [Math]::Min(256 / $source.Width, 256 / $source.Height)
    $width = [int]($source.Width * $scale)
    $height = [int]($source.Height * $scale)
    $x = [int]((256 - $width) / 2)
    $y = [int]((256 - $height) / 2)
    $graphics.DrawImage($source, $x, $y, $width, $height)

    $AppIcon = [System.Drawing.Icon]::FromHandle($bitmap.GetHicon())
    $stream = [System.IO.File]::Create($IconPath)
    $AppIcon.Save($stream)
    $stream.Close()

    $graphics.Dispose()
    $bitmap.Dispose()
    $AppIcon.Dispose()
    $source.Dispose()
} elseif (-not (Test-Path $IconPath)) {
    $bitmap = [System.Drawing.Bitmap]::new(256, 256)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.Clear([System.Drawing.Color]::FromArgb(18, 24, 38))

    $brush = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.Rectangle]::new(0, 0, 256, 256),
        [System.Drawing.Color]::FromArgb(34, 197, 94),
        [System.Drawing.Color]::FromArgb(14, 165, 233),
        45
    )
    $graphics.FillRectangle($brush, 18, 18, 220, 220)

    $font = [System.Drawing.Font]::new("Segoe UI", 82, [System.Drawing.FontStyle]::Bold)
    $textBrush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
    $format = [System.Drawing.StringFormat]::new()
    $format.Alignment = [System.Drawing.StringAlignment]::Center
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $graphics.DrawString("EH", $font, $textBrush, [System.Drawing.RectangleF]::new(0, 0, 256, 256), $format)

    $AppIcon = [System.Drawing.Icon]::FromHandle($bitmap.GetHicon())
    $stream = [System.IO.File]::Create($IconPath)
    $AppIcon.Save($stream)
    $stream.Close()

    $format.Dispose()
    $textBrush.Dispose()
    $font.Dispose()
    $brush.Dispose()
    $graphics.Dispose()
    $bitmap.Dispose()
    $AppIcon.Dispose()
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "Elhacker" `
    --icon "$IconPath" `
    --add-data "$Assets;assets" `
    @ModeArgs `
    "downloader.py"

Write-Host ""
if ($OneFile) {
    Write-Host "Built: dist\Elhacker.exe"
} else {
    Write-Host "Built: dist\Elhacker\Elhacker.exe"
}
