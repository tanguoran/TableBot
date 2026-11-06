param(
    [ValidateRange(0, 100000)] [int]$Tail = 50,
    [switch]$All,
    [switch]$Raw,
    [switch]$Once
)

$monitorArguments = @('-m', 'jarvis.monitor', '--tail', "$Tail")
if ($All) { $monitorArguments += '--all' }
if ($Raw) { $monitorArguments += '--raw' }
if ($Once) { $monitorArguments += '--once' }
& wsl.exe --distribution Ubuntu --cd $PSScriptRoot --exec python3 @monitorArguments
exit $LASTEXITCODE
