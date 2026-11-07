# 一键推送到 GitHub（本机只有 WSL 的 git，脚本里走 wsl）
#
# 用法：
#   $env:GH_TOKEN = "ghp_xxx"      # classic PAT，勾 repo 权限
#   pwsh -NoProfile -File C:\h\hippo\scripts\push-github.ps1
#   # 可选：-Owner 你的账号 -Repo 仓库名 -Private
param(
  [string]$Owner = "",
  [string]$Repo = "hippocampus-deepseek",
  [switch]$Private,
  [string]$Branch = "master"
)

$ErrorActionPreference = "Stop"
$token = $env:GH_TOKEN
if (-not $token) { throw "先设置环境变量 GH_TOKEN（GitHub Personal Access Token，需 repo 权限）" }

$headers = @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json"; "User-Agent" = "hippo-push" }
if (-not $Owner) {
  $me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $headers -Method Get
  $Owner = $me.login
}
Write-Host "目标仓库：$Owner/$Repo"

$exists = $true
try { Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$Repo" -Headers $headers -Method Get | Out-Null }
catch { $exists = $false }

if (-not $exists) {
  $body = @{
    name = $Repo
    private = [bool]$Private
    description = "DSH 记忆插件：本地长期记忆 + 三维看板 + QQ 桥接"
  } | ConvertTo-Json
  Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Headers $headers -Method Post -Body $body | Out-Null
  Write-Host ("已创建仓库（" + $(if ($Private) { "private" } else { "public" }) + "）")
} else {
  Write-Host "仓库已存在，直接推送"
}

# 用 Basic 头推送：token 不进 remote、不落 .git/config
$auth = "x-access-token:$token"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($auth))
$remote = "https://github.com/$Owner/$Repo.git"
$script = "cd /mnt/c/h/hippo && git remote remove origin 2>/dev/null; " +
          "git remote add origin '$remote' && git branch -M $Branch && " +
          "git -c http.extraheader='Authorization: Basic $b64' push origin $Branch --tags"

wsl -e bash -lc $script
if ($LASTEXITCODE -ne 0) { throw "推送失败（退出码 $LASTEXITCODE）" }
Write-Host "推送完成： https://github.com/$Owner/$Repo" -ForegroundColor Green
