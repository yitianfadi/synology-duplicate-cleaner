# 便捷包装：在 Windows PowerShell 中构建 SPK
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
python (Join-Path $dir "build_spk.py") @args
