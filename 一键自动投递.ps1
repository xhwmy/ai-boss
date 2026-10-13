$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project '.venv\Scripts\python.exe'
$app = Join-Path $project 'app.py'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$logs = Join-Path $project 'logs'

if (-not (Test-Path -LiteralPath $python)) { throw '未找到项目虚拟环境，请先安装依赖。' }
if (-not (Get-Process chrome -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path -LiteralPath $chrome)) { throw '未找到 Chrome 浏览器。' }
    Start-Process -FilePath $chrome -ArgumentList 'https://www.zhipin.com/web/geek/jobs'
}
New-Item -ItemType Directory -Path $logs -Force | Out-Null
Start-Process -FilePath $python -ArgumentList $app -WorkingDirectory $project `
    -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'flask_stdout.log') `
    -RedirectStandardError (Join-Path $logs 'flask_stderr.log')

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/status' -TimeoutSec 2
        if ($status.status) { $ready = $true; break }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) { throw '控制面板未能启动，请查看 logs\flask_stderr.log。' }
Start-Process 'http://127.0.0.1:5000/'
