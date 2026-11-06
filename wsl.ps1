param(
    [ValidateSet('run', 'check', 'test', 'smoke', 'pull', 'search-test', 'kb-index', 'kb-smoke', 'vision-smoke', 'desktop-bridge', 'desktop-mcp', 'desktop-smoke', 'monitor')]
    [string]$Action = 'run'
)

# Keep the existing Windows source directory and execute with Linux Python.
$jarvisInputEncoding = [Console]::InputEncoding
$jarvisOutputEncoding = [Console]::OutputEncoding
$jarvisPipeEncoding = $OutputEncoding
$jarvisExitCode = 1
try {
    $jarvisUtf8 = [System.Text.UTF8Encoding]::new($false)
    [Console]::InputEncoding = $jarvisUtf8
    [Console]::OutputEncoding = $jarvisUtf8
    $OutputEncoding = $jarvisUtf8
    & wsl.exe --distribution Ubuntu --cd $PSScriptRoot --exec bash ./scripts/wsl.sh $Action
    $jarvisExitCode = $LASTEXITCODE
}
finally {
    [Console]::InputEncoding = $jarvisInputEncoding
    [Console]::OutputEncoding = $jarvisOutputEncoding
    $OutputEncoding = $jarvisPipeEncoding
}
exit $jarvisExitCode

