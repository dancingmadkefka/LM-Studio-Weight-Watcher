# Advance only if the current clean Git state exactly matches the reviewed commit/tree/report.
# Usage: .cursor/scripts/advance-milestone.ps1 [-ValidateOnly]

param(
    [switch]$ValidateOnly,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
    Write-Host "Usage: advance-milestone.ps1 [-ValidateOnly]"
    exit 0
}

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & git @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed:`n$($output -join "`n")"
    }
    return $output
}

$RepoRoot = (Invoke-Git rev-parse --show-toplevel | Select-Object -First 1).Trim()
Set-Location $RepoRoot
$StatePath = Join-Path $RepoRoot ".cursor\workflow-state.json"
if (-not (Test-Path -LiteralPath $StatePath)) { throw "Missing workflow state: $StatePath" }

$state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
$dirty = @(Invoke-Git status --porcelain=v1 --untracked-files=all)
if ($dirty.Count -gt 0) {
    throw "Cannot advance with a dirty worktree:`n$($dirty -join "`n")"
}
if ([string]$state.status -ne "review_clean") {
    throw "Cannot advance: status is '$($state.status)', expected 'review_clean'."
}
if ([int]$state.clean_review_passes -lt [int]$state.required_clean_review_passes) {
    throw "Cannot advance: need $($state.required_clean_review_passes) clean pass(es)."
}

$head = (Invoke-Git rev-parse HEAD | Select-Object -First 1).Trim()
$tree = (Invoke-Git rev-parse "HEAD^{tree}" | Select-Object -First 1).Trim()
if ($head -ne [string]$state.candidate_commit -or $head -ne [string]$state.reviewed_commit) {
    throw "HEAD $head does not match candidate/reviewed commit."
}
if ($tree -ne [string]$state.reviewed_tree) {
    throw "Current tree $tree does not match reviewed tree $($state.reviewed_tree)."
}
if (-not $state.last_review_file -or -not (Test-Path -LiteralPath $state.last_review_file)) {
    throw "Review report is missing."
}
$reportHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $state.last_review_file).Hash.ToLowerInvariant()
if ($reportHash -ne [string]$state.last_review_sha256) {
    throw "Review report hash mismatch."
}
if ([string]$state.human_validation_status -eq "pending") {
    throw "Required human validation remains pending."
}

Write-Host "Validated review binding for $($state.current_milestone) at $head."
if ($ValidateOnly) { exit 0 }

$order = @($state.milestone_order)
$idx = [array]::IndexOf($order, [string]$state.current_milestone)
if ($idx -lt 0) { throw "Current milestone is not in milestone_order." }

$state.baseline_commit = $head
if ($idx -ge ($order.Length - 1)) {
    $state.status = "project_complete"
    $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $StatePath -Encoding utf8
    Write-Host "Project complete. All milestones are signed off."
    exit 0
}

$next = $order[$idx + 1]
$state.current_milestone = $next
$state.milestone_name = $state.milestones.$next
$state.status = "not_started"
$state.review_turn = 0
$state.clean_review_passes = 0
$state.candidate_commit = $null
$state.reviewed_commit = $null
$state.reviewed_tree = $null
$state.last_review_verdict = $null
$state.last_review_file = $null
$state.last_review_sha256 = $null
$state.last_review_at = $null
$state.last_reviewer_model = $null
$state.last_reviewer_reasoning_effort = $null
$state.human_validation_status = "not_required"

$state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $StatePath -Encoding utf8
Write-Host "Advanced to $next - $($state.milestone_name)"
