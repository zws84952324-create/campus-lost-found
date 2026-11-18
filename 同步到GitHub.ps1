param([Parameter(Mandatory=$true)][string]$说明)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
git add --all
if ($LASTEXITCODE -ne 0) { throw '暂存修改失败。' }
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { Write-Output '没有新的修改需要提交。'; exit 0 }
git commit -m $说明
if ($LASTEXITCODE -ne 0) { throw '提交失败，请查看上方提示。' }
git push
if ($LASTEXITCODE -ne 0) { throw '提交已保存在本地，但同步失败。网络恢复后执行 git push 即可。' }
Write-Output '已同步。GitHub 的提交记录可以查看本次修改内容和时间。'
