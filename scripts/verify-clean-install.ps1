# M28.1 干净环境验证：全新 venv 安装 → 三命令可用 → uninstall 无残留
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\27203\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$venv = "C:\h\.venv-clean"
$src = "C:\h\hippo"
$t2 = "C:\h\t2"

if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }
if (Test-Path $t2) { Remove-Item $t2 -Recurse -Force }

Write-Host "== 1) fresh venv =="
& $py -m venv $venv
$vpy = "$venv\Scripts\python.exe"
$hx = "$venv\Scripts\hippo.exe"

Write-Host "== 2) pip install (non-editable) =="
& $vpy -m pip install --quiet --disable-pip-version-check $src 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "== 3) hippo --version =="
& $hx --version
$versionOk = ($LASTEXITCODE -eq 0)

Write-Host "== 4) init =="
& $hx init --dir $t2
Write-Host "== 5) doctor =="
& $hx doctor --dir $t2
$doctorOk = ($LASTEXITCODE -eq 0)

Write-Host "== 6) serve smoke =="
$outFile = "$venv\serve.out"
$errFile = "$venv\serve.err"
$proc = Start-Process -FilePath $hx -ArgumentList "serve","--dir",$t2,"--port","8799" -PassThru -NoNewWindow -RedirectStandardOutput $outFile -RedirectStandardError $errFile
Start-Sleep -Seconds 2
$health = $null
for ($i = 0; $i -lt 20; $i++) {
  try { $health = Invoke-WebRequest -Uri "http://127.0.0.1:8799/health" -UseBasicParsing -TimeoutSec 2; break } catch { Start-Sleep -Milliseconds 500 }
}
if ($health) { Write-Host "HEALTH $($health.StatusCode): $($health.Content)" } else { Write-Host "HEALTH FAILED" }
Write-Host "-- serve stdout --"
Get-Content $outFile -ErrorAction SilentlyContinue | Select-Object -First 14
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 300
$serveOk = ($null -ne $health)

Write-Host "== 7) uninstall + residual scan =="
& $vpy -m pip uninstall -y hippocampus-deepseek 2>&1 | Out-Host
$sp = "$venv\Lib\site-packages"
$resid = @()
$resid += Get-ChildItem $sp -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "^(hippo|hippocampus)" } | ForEach-Object { "site-packages/$($_.Name)" }
$resid += Get-ChildItem "$venv\Scripts" -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "^hippo" } | ForEach-Object { "Scripts/$($_.Name)" }

Write-Host ""
Write-Host "== RESULT =="
"version_ok  = $versionOk"
"doctor_ok   = $doctorOk"
"serve_ok    = $serveOk"
"residual    = $($resid -join ', ')"
"clean       = $($resid.Count -eq 0)"
