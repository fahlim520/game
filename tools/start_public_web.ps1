$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null

$python = Join-Path $root '.venv\Scripts\python.exe'
$cloudflared = Join-Path $env:USERPROFILE '.cache\codex-runtimes\cloudflared.exe'
$serverOut = Join-Path $runtime 'web-server.out.log'
$serverErr = Join-Path $runtime 'web-server.err.log'
$tunnelOut = Join-Path $runtime 'cloudflared.out.log'
$tunnelErr = Join-Path $runtime 'cloudflared.err.log'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python runtime not found: $python"
}
if (-not (Test-Path -LiteralPath $cloudflared)) {
    throw "cloudflared not found: $cloudflared"
}

$tokenFile = Join-Path $runtime 'web-access-token.txt'
$serverProcess = $null
$existingHealth = $null
try {
    $existingHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 2
} catch {
    $existingHealth = $null
}

if ($existingHealth -and $existingHealth.ok) {
    if (-not (Test-Path -LiteralPath $tokenFile)) {
        throw "Port 8000 is already serving a web app, but $tokenFile does not exist."
    }
    $token = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
} else {
    $token = [guid]::NewGuid().ToString('N')
    $token | Set-Content -LiteralPath $tokenFile -Encoding utf8

$serverProcess = Start-Process -FilePath $python `
        -ArgumentList @('-u', 'web_server.py', '--host', '127.0.0.1', '--port', '8000', '--access-token', $token, '--target-enemies', '30') `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $serverOut `
        -RedirectStandardError $serverErr `
    -PassThru

    $ready = $false
    for ($index = 0; $index -lt 40; $index++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 2
            if ($health.ok) {
                $ready = $true
                break
            }
        } catch {
            if ($serverProcess.HasExited) {
                throw "Web server exited. Check $serverErr"
            }
        }
    }
    if (-not $ready) {
        throw "Web server did not become ready. Check $serverErr"
    }
}

$existingTunnels = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq 'cloudflared.exe' -and
        $_.CommandLine -like '*--url http://127.0.0.1:8000*'
    }
foreach ($tunnel in $existingTunnels) {
    Stop-Process -Id $tunnel.ProcessId -Force -ErrorAction SilentlyContinue
}

$tunnelProcess = Start-Process -FilePath $cloudflared `
    -ArgumentList @('tunnel', '--url', 'http://127.0.0.1:8000', '--protocol', 'http2', '--no-autoupdate') `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $tunnelOut `
    -RedirectStandardError $tunnelErr `
    -PassThru

$publicUrl = $null
for ($index = 0; $index -lt 80; $index++) {
    Start-Sleep -Milliseconds 500
    $logs = @()
    if (Test-Path -LiteralPath $tunnelOut) {
        $logs += Get-Content -LiteralPath $tunnelOut -Raw -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $tunnelErr) {
        $logs += Get-Content -LiteralPath $tunnelErr -Raw -ErrorAction SilentlyContinue
    }
    $combined = $logs -join "`n"
    $match = [regex]::Match($combined, 'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
    if ($match.Success) {
        $publicUrl = $match.Value
        break
    }
    if ($tunnelProcess.HasExited) {
        throw "Cloudflare tunnel exited. Check $tunnelErr"
    }
}
if (-not $publicUrl) {
    throw "Cloudflare URL was not created. Check $tunnelErr"
}

$link = "$publicUrl/?token=$token"
$link | Set-Content -LiteralPath (Join-Path $runtime 'public-link.txt') -Encoding utf8

Write-Output "PUBLIC_URL=$link"
if ($serverProcess) {
    Write-Output "SERVER_PID=$($serverProcess.Id)"
} else {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    Write-Output "SERVER_PID=$($listener.OwningProcess)"
}
Write-Output "TUNNEL_PID=$($tunnelProcess.Id)"
