param([Parameter(Mandatory=$true)][string]$Python,[Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference='Stop'
& $Python (Join-Path $PSScriptRoot 'manage.py') @Arguments
if($LASTEXITCODE -ne 0){throw 'Management operation failed; inspect private logs.'}
