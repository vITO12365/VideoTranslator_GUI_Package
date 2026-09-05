param(
    [switch]$WithOCR
)

$ErrorActionPreference = "Stop"

function Find-Ollama {
    $command = Get-Command ollama -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        (Join-Path $env:ProgramFiles "Ollama\ollama.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " VideoTranslator AI 模型安裝程式" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host

$ollamaExe = Find-Ollama
if (-not $ollamaExe) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "找不到 Ollama，也找不到 winget。" -ForegroundColor Red
        Write-Host "請先從 https://ollama.com/download/windows 安裝 Ollama，再重新執行本檔案。"
        exit 1
    }

    Write-Host ">>> 尚未安裝 Ollama，現在透過 winget 安裝……" -ForegroundColor Yellow
    & $winget.Source install --id Ollama.Ollama -e `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Ollama 安裝失敗（代碼 $LASTEXITCODE）"
    }

    $ollamaExe = Find-Ollama
    if (-not $ollamaExe) {
        throw "Ollama 已完成安裝，但暫時找不到執行檔。請重新開機後再執行一次。"
    }
}

Write-Host ">>> Ollama：$ollamaExe" -ForegroundColor Green

& $ollamaExe list *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host ">>> 正在啟動 Ollama 背景服務……"
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    $ready = $false
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        Start-Sleep -Seconds 1
        & $ollamaExe list *> $null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        throw "Ollama 背景服務無法啟動。請開啟 Ollama 後再重試。"
    }
}

$requiredModels = @(
    "hf.co/SakuraLLM/Sakura-14B-Qwen2.5-v1.0-GGUF",
    "qwen3:8b"
)

foreach ($model in $requiredModels) {
    Write-Host
    Write-Host ">>> 下載／更新：$model" -ForegroundColor Cyan
    & $ollamaExe pull $model
    if ($LASTEXITCODE -ne 0) {
        throw "模型下載失敗：$model"
    }
}

if (-not $WithOCR) {
    Write-Host
    $answer = Read-Host "是否安裝畫面字幕 OCR 模型 minicpm-v4.5:latest？檔案較大 (y/N)"
    $WithOCR = $answer -match "^(y|yes)$"
}

if ($WithOCR) {
    Write-Host
    Write-Host ">>> 下載／更新：minicpm-v4.5:latest" -ForegroundColor Cyan
    & $ollamaExe pull "minicpm-v4.5:latest"
    if ($LASTEXITCODE -ne 0) {
        throw "OCR 模型下載失敗：minicpm-v4.5:latest"
    }
}

Write-Host
Write-Host "========================================" -ForegroundColor Green
Write-Host " AI 模型安裝完成" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
& $ollamaExe list
