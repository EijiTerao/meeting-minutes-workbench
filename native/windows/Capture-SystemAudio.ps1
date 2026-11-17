[CmdletBinding()]
param(
    [switch] $ListEndpoints,
    [string] $EndpointId,
    [string] $OutputDirectory,
    [switch] $SelfTest
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'SystemAudioCapture.cs'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Capture source not found: $source" }
Add-Type -Path $source

if ($SelfTest) {
    [WorkbenchSystemAudio]::SelfTest()
    Write-Output 'SelfTest passed: PCM conversion, WAV layout, and completion contract.'
    exit 0
}

if ($ListEndpoints) {
    [WorkbenchSystemAudio]::ListEndpoints() | ForEach-Object { Write-Output $_ }
    exit 0
}

if ([string]::IsNullOrWhiteSpace($EndpointId) -or [string]::IsNullOrWhiteSpace($OutputDirectory)) {
    throw 'Usage: .\Capture-SystemAudio.ps1 -ListEndpoints, or -EndpointId <ID> -OutputDirectory <existing-empty-directory>'
}
if (-not [WorkbenchSystemAudio]::IsActiveRenderEndpoint($EndpointId)) {
    throw 'EndpointId must exactly match an active render endpoint listed by -ListEndpoints. The default device is never selected automatically.'
}

$resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedOutput -PathType Container)) { throw 'OutputDirectory must already exist.' }
if (@(Get-ChildItem -LiteralPath $resolvedOutput -Force | Select-Object -First 1).Count -ne 0) {
    throw 'OutputDirectory must be empty. Existing data is never overwritten or removed.'
}

Write-Warning 'This records all audio rendered to the selected output device, including other apps and system sounds. It does not capture the microphone or video, and it does not isolate an app.'
Write-Host "Selected endpoint: $EndpointId"
Write-Host "Save directory: $resolvedOutput"
Write-Host 'Type CAPTURE to start; Ctrl+C stops and finalizes the recording.'
if ((Read-Host 'Consent') -cne 'CAPTURE') { throw 'Capture was not started; explicit consent was not given.' }

[WorkbenchSystemAudio]::Capture($EndpointId, $resolvedOutput)
Write-Output 'Capture stopped normally and recording-complete.json was written.'
