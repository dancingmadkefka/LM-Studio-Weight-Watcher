# Review the exact clean HEAD commit in a fresh Codex session and bind PASS to that tree.
# Usage: .cursor/scripts/run-codex-review.ps1 [-Milestone M0] [-Model gpt-5.6-sol] [-ReasoningEffort high]

param(
    [string]$Milestone = "",
    [string]$Model = "",
    [ValidateSet("minimal", "low", "medium", "high", "xhigh")]
    [string]$ReasoningEffort = "",
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
    Write-Host "Usage: run-codex-review.ps1 [-Milestone M0] [-Model gpt-5.6-sol] [-ReasoningEffort high]"
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

function Save-State {
    param($State, [string]$Path)
    $State | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Path -Encoding utf8
}

$RepoRoot = (Invoke-Git rev-parse --show-toplevel | Select-Object -First 1).Trim()
Set-Location $RepoRoot

$StatePath = Join-Path $RepoRoot ".cursor\workflow-state.json"
$ReviewsDir = Join-Path $RepoRoot ".cursor\reviews"
$SchemaPath = Join-Path $RepoRoot ".cursor\scripts\review-result.schema.json"

foreach ($required in @($StatePath, $SchemaPath, (Join-Path $RepoRoot "REVIEW.md"))) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing workflow file: $required"
    }
}

$state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
if ([int]$state.schema_version -ne 2) {
    throw "Unsupported workflow-state schema: $($state.schema_version)"
}

if (-not $Milestone) { $Milestone = [string]$state.current_milestone }
if ($Milestone -ne [string]$state.current_milestone) {
    throw "Requested milestone $Milestone is not current milestone $($state.current_milestone)."
}

$milestoneName = $state.milestones.$Milestone
if (-not $milestoneName) { throw "Unknown milestone: $Milestone" }

if (-not $Model) { $Model = [string]$state.reviewer_model }
if (-not $Model) { $Model = "gpt-5.6-sol" }
if (-not $ReasoningEffort) {
    if ($state.PSObject.Properties.Name -contains "reviewer_reasoning_effort" -and $state.reviewer_reasoning_effort) {
        $ReasoningEffort = [string]$state.reviewer_reasoning_effort
    } else {
        $ReasoningEffort = "high"
    }
}

if (
    [string]$state.review_policy -eq "independent_required" -and
    [string]$state.builder_identity -eq [string]$state.reviewer_identity
) {
    throw "Independent review required, but builder and reviewer identities are both '$($state.builder_identity)'."
}

foreach ($doc in @($state.authority_documents) + @($state.review_document)) {
    $path = Join-Path $RepoRoot ([string]$doc)
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing authority/review document: $doc"
    }
}

$dirtyBefore = @(Invoke-Git status --porcelain=v1 --untracked-files=all)
if ($dirtyBefore.Count -gt 0) {
    throw "Refusing mixed-scope review: worktree is not clean.`n$($dirtyBefore -join "`n")"
}

$candidate = (Invoke-Git rev-parse HEAD | Select-Object -First 1).Trim()
$tree = (Invoke-Git rev-parse "$candidate^{tree}" | Select-Object -First 1).Trim()

if ($state.baseline_commit -and $candidate -eq [string]$state.baseline_commit) {
    throw "HEAD is still the bootstrap/baseline commit; create the milestone candidate commit first."
}
if (-not $state.baseline_commit) {
    throw "Workflow baseline_commit is not configured."
}
$parentLine = (Invoke-Git rev-list --parents -n 1 $candidate | Select-Object -First 1).Trim()
$parentParts = @($parentLine -split '\s+')
if ($parentParts.Count -ne 2) {
    throw "Candidate must be one non-merge checkpoint commit on top of the reviewed baseline."
}
if ($parentParts[1] -ne [string]$state.baseline_commit) {
    throw "Candidate parent $($parentParts[1]) does not match workflow baseline $($state.baseline_commit). Amend/squash the current milestone to one checkpoint commit."
}

$state.candidate_commit = $candidate
$state.status = "awaiting_review"
Save-State $state $StatePath

New-Item -ItemType Directory -Force -Path $ReviewsDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$baseName = "$Milestone-$timestamp-$($candidate.Substring(0, 12))"
$JsonFile = Join-Path $ReviewsDir "$baseName.json"
$ReportFile = Join-Path $ReviewsDir "$baseName.md"
$LogFile = Join-Path $ReviewsDir "$baseName.log"

$CodexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($CodexCommand) {
    $CodexExe = $CodexCommand.Source
} else {
    $CodexExe = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin\codex.exe"
}
if (-not (Test-Path -LiteralPath $CodexExe)) {
    throw "Codex CLI not found. Install Codex or add codex to PATH."
}

$authorityList = (@($state.authority_documents) | ForEach-Object { "- $_" }) -join "`n"
$Prompt = @"
Act as the independent milestone reviewer for $($state.project_name).

Milestone: $Milestone - $milestoneName
Candidate commit: $candidate
Builder identity: $($state.builder_identity)
Reviewer identity: $($state.reviewer_identity)

Authoritative build documents:
$authorityList
Review protocol: $($state.review_document)

Review only the change introduced by candidate commit $candidate. Use git show and the
candidate's parent to establish scope. Follow REVIEW.md section 0 and the $Milestone
checklist exactly.

Requirements:
- Use high-scrutiny reasoning. Prefer executing checks over inferring from source.
- Run every applicable automated check. Source inspection alone cannot establish PASS.
- Cite commands, outputs, file paths, line numbers, measurements, or supplied evidence.
- Use PASS, FAIL, or UNVERIFIED for every checklist item.
- Any FAIL or UNVERIFIED MUST is a blocker.
- Mark blockers that require audible/visual/hardware/user evidence as kind=human.
- Flag scope creep and contradictions as kind=spec.
- Do not modify source, configuration, authority documents, or Git history.
- Build/test artifacts are allowed only where ignored by Git.
- Return only JSON conforming to the supplied output schema.
- Set reviewer_model in the JSON to exactly: $Model
"@

$reviewArgs = @(
    "exec",
    "--ephemeral",
    "--ignore-user-config",
    "--sandbox", "workspace-write",
    "--model", $Model,
    "-c", "model_reasoning_effort=`"$ReasoningEffort`"",
    "--cd", $RepoRoot,
    "--output-schema", $SchemaPath,
    "--output-last-message", $JsonFile,
    "-"
)

Write-Host "Reviewing $Milestone commit $candidate with $Model (reasoning=$ReasoningEffort)..."

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $Prompt | & $CodexExe @reviewArgs 2>&1 | Tee-Object -FilePath $LogFile
    $exitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorAction
}

if ($exitCode -ne 0) {
    $state.status = "needs_fixes"
    Save-State $state $StatePath
    throw "Codex review exited with code $exitCode. See $LogFile"
}
if (-not (Test-Path -LiteralPath $JsonFile)) {
    throw "Review output was not created: $JsonFile"
}

try {
    $result = Get-Content -Raw -LiteralPath $JsonFile | ConvertFrom-Json
} catch {
    $state.status = "needs_fixes"
    Save-State $state $StatePath
    throw "Review output is not valid schema-conformant JSON: $($_.Exception.Message)"
}
$blockers = @($result.blockers)
$checks = @($result.checks)

if ([string]$result.milestone -ne $Milestone) {
    $blockers += [pscustomobject]@{
        id = "REVIEW-MILESTONE-MISMATCH"
        kind = "spec"
        severity = "MUST"
        description = "Reviewer returned milestone '$($result.milestone)' instead of '$Milestone'."
        evidence = @($JsonFile)
    }
}
if ([string]$result.candidate_commit -ne $candidate) {
    $blockers += [pscustomobject]@{
        id = "REVIEW-COMMIT-MISMATCH"
        kind = "spec"
        severity = "MUST"
        description = "Reviewer did not bind its result to candidate commit $candidate."
        evidence = @($JsonFile)
    }
}

$dirtyAfter = @(Invoke-Git status --porcelain=v1 --untracked-files=all)
if ($dirtyAfter.Count -gt 0) {
    $blockers += [pscustomobject]@{
        id = "REVIEW-DIRTY-WORKTREE"
        kind = "code"
        severity = "MUST"
        description = "Review execution changed the Git worktree."
        evidence = @($dirtyAfter)
    }
}

$blockingChecks = @($checks | Where-Object {
    [string]$_.requirement_level -eq "MUST" -and [string]$_.status -ne "PASS"
})
$isPass = (
    [string]$result.verdict -eq "PASS" -and
    $blockers.Count -eq 0 -and
    $blockingChecks.Count -eq 0
)

$verdict = if ($isPass) { "PASS" } else { "NEEDS-REVISION" }
$blockingItemCount = $blockers.Count + $blockingChecks.Count
$nonHumanBlockingCount = (
    @($blockers | Where-Object { [string]$_.kind -ne "human" }).Count +
    @($blockingChecks | Where-Object { [string]$_.kind -ne "human" }).Count
)
$onlyHuman = (-not $isPass -and $blockingItemCount -gt 0 -and $nonHumanBlockingCount -eq 0)

$lines = @(
    "# $Milestone Review - $milestoneName",
    "",
    "- Candidate: ``$candidate``",
    "- Tree: ``$tree``",
    "- Reviewer: $($state.reviewer_identity) / $Model",
    "- Verdict: **$verdict**",
    "- Generated: $((Get-Date).ToUniversalTime().ToString("o"))",
    "",
    "## Summary",
    "",
    [string]$result.summary,
    "",
    "## Checks",
    ""
)
foreach ($check in $checks) {
    $lines += "- **$($check.status)** [$($check.requirement_level)] $($check.id): $($check.description)"
    foreach ($evidence in @($check.evidence)) { $lines += "  - Evidence: $evidence" }
}
$lines += @("", "## Blockers", "")
if ($blockers.Count -eq 0) {
    $lines += "None."
} else {
    foreach ($blocker in $blockers) {
        $lines += "- [$($blocker.kind)/$($blocker.severity)] $($blocker.id): $($blocker.description)"
        foreach ($evidence in @($blocker.evidence)) { $lines += "  - Evidence: $evidence" }
    }
}
$lines += @("", "## Deferred suggestions", "")
if (@($result.deferred).Count -eq 0) {
    $lines += "None."
} else {
    foreach ($item in @($result.deferred)) { $lines += "- $item" }
}
$lines += @(
    "",
    "REVIEW_SUMMARY verdict=$verdict substantial=$(if ($isPass) { 'no' } else { 'yes' }) blockers=$($blockers.Count + $blockingChecks.Count)"
)
$lines | Set-Content -LiteralPath $ReportFile -Encoding utf8

$reportHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ReportFile).Hash.ToLowerInvariant()
$state.review_turn = [int]$state.review_turn + 1
$state.reviewed_commit = $candidate
$state.reviewed_tree = $tree
$state.last_review_verdict = $verdict
$state.last_review_file = $ReportFile
$state.last_review_sha256 = $reportHash
$state.last_review_at = (Get-Date).ToUniversalTime().ToString("o")
$state.last_reviewer_model = $Model
if ($state.PSObject.Properties.Name -contains "last_reviewer_reasoning_effort") {
    $state.last_reviewer_reasoning_effort = $ReasoningEffort
} else {
    $state | Add-Member -NotePropertyName last_reviewer_reasoning_effort -NotePropertyValue $ReasoningEffort -Force
}

if ($isPass) {
    $state.clean_review_passes = [int]$state.clean_review_passes + 1
    $state.status = "review_clean"
    $state.human_validation_status = "passed"
} elseif ($onlyHuman) {
    $state.clean_review_passes = 0
    $state.status = "awaiting_human_validation"
    $state.human_validation_status = "pending"
} else {
    $state.clean_review_passes = 0
    $state.status = "needs_fixes"
}
Save-State $state $StatePath

Write-Host "Review complete: $verdict"
Write-Host "Blockers: $($blockers.Count + $blockingChecks.Count)"
Write-Host "Report: $ReportFile"
if ($isPass) {
    Write-Host "MILESTONE_READY: run advance-milestone.ps1"
}
