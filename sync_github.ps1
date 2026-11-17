param(
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
$git = if ($gitCmd) { $gitCmd.Source } else { "C:\Program Files\Git\bin\git.exe" }

if (-not (Test-Path $git)) {
    Write-Host "오류: Git을 찾을 수 없습니다. ($git)" -ForegroundColor Red
    exit 1
}

$branch = (& $git branch --show-current).Trim()
if (-not $branch) {
    Write-Host "오류: 현재 Git 브랜치를 확인할 수 없습니다." -ForegroundColor Red
    exit 1
}

Write-Host "=== GitHub 동기화 시작 (브랜치: $branch) ===" -ForegroundColor Cyan

$status = & $git status --porcelain
if ($status) {
    if ([string]::IsNullOrWhiteSpace($Message)) {
        $inputMsg = Read-Host "커밋 메시지를 입력하세요 (그냥 Enter 시 자동 한글 메시지 생성)"
        if ([string]::IsNullOrWhiteSpace($inputMsg)) {
            $now = Get-Date -Format "yyyy-MM-dd HH:mm"
            $Message = "작업 내역 저장 ($now)"
        } else {
            $Message = $inputMsg.Trim()
        }
    }

    & $git add -A
    & $git commit -m $Message
    if ($LASTEXITCODE -ne 0) {
        Write-Host "오류: 커밋에 실패했습니다." -ForegroundColor Red
        exit 1
    }
    Write-Host "커밋 완료: $Message" -ForegroundColor Green
} else {
    Write-Host "새로 커밋할 변경사항이 없습니다." -ForegroundColor Yellow
}

Write-Host "원격 저장소에서 최신 변경사항을 가져오는 중..." -ForegroundColor Cyan
& $git pull --rebase origin $branch
if ($LASTEXITCODE -ne 0) {
    Write-Host "충돌 또는 Pull 오류가 발생했습니다. 충돌 해결 후 다시 시도해 주세요." -ForegroundColor Red
    exit 1
}

Write-Host "GitHub에 업로드하는 중..." -ForegroundColor Cyan
& $git push -u origin $branch
if ($LASTEXITCODE -ne 0) {
    Write-Host "오류: Push에 실패했습니다." -ForegroundColor Red
    exit 1
}

Write-Host "=== GitHub 동기화가 완료되었습니다! ===" -ForegroundColor Green
