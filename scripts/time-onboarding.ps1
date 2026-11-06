# M28.2 从零计时：全新 venv → pip install → init → serve → health
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\27203\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$venv = "C:\h\.venv-onboard"
$t3 = "C:\h\t3"

if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }
if (Test-Path $t3) { Remove-Item $t3 -Recurse -Force }

$sw = [Diagnostics.Stopwatch]::StartNew()
$marks = [System.Collections.Generic.List[string]]::new()

& $py -m venv $venv
$marks.Add(("venv 创建完成        {0,6:N1}s" -f $sw.Elapsed.TotalSeconds))

$vpy = "$venv\Scripts\python.exe"
& $vpy -m pip install -q --disable-pip-version-check "C:\h\hippo" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
$marks.Add(("pip install 完成     {0,6:N1}s" -f $sw.Elapsed.TotalSeconds))

& "$venv\Scripts\hippo.exe" init --dir $t3 | Out-Null
$marks.Add(("hippo init 完成      {0,6:N1}s" -f $sw.Elapsed.TotalSeconds))

$proc = Start-Process "$venv\Scripts\hippo.exe" -ArgumentList "serve","--dir",$t3,"--port","8802" -PassThru -NoNewWindow -RedirectStandardOutput "$venv\o.out" -RedirectStandardError "$venv\o.err"
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
  try {
    $h = Invoke-WebRequest "http://127.0.0.1:8802/health" -UseBasicParsing -TimeoutSec 2
    $ok = ($h.StatusCode -eq 200)
    break
  } catch { Start-Sleep -Milliseconds 250 }
}
$marks.Add(("serve + /health     {0,6:N1}s" -f $sw.Elapsed.TotalSeconds))
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
$sw.Stop()

$marks
"-----"
"TOTAL_SECONDS = {0:N1}" -f $sw.Elapsed.TotalSeconds
"HEALTH_OK = $ok"
