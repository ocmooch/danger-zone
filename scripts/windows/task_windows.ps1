# Shows or hides the console windows of the two weekly danger-zone tasks (dz-sleeper-capture,
# dz-vault-sync). Run it through scripts/windows/task_windows.sh from WSL. See docs/RUNBOOK.md
# "Window mode".
#
#   Hidden  : Task Scheduler starts `conhost.exe --headless wsl.exe ...`, so no window opens.
#   Visible : it starts `wsl.exe ...` directly, and the window shows live progress.
#   Status  : prints each task's mode, command, last run and last result.
#
# Only the Execute/Arguments of the existing action and the task Description change. The
# trigger, settings and user stay as registered.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Hidden", "Visible", "Status")]
    [string]$Mode
)
$ErrorActionPreference = "Stop"
$tasks = @(
    @{ Name = "dz-sleeper-capture"; What = "weekly Sleeper capture + load + score" },
    @{ Name = "dz-vault-sync"; What = "weekly off-site vault backup" }
)
foreach ($t in $tasks) {
    $task = Get-ScheduledTask -TaskName $t.Name
    $action = $task.Actions[0]
    # The WSL part of the command, whichever mode the task is in now.
    $wslArgs = if ($action.Execute -match "conhost") {
        $action.Arguments -replace '^\s*--headless\s+wsl\.exe\s+', ''
    } else {
        $action.Arguments
    }
    $current = if ($action.Execute -match "conhost") { "Hidden" } else { "Visible" }
    if ($Mode -eq "Status") {
        $info = Get-ScheduledTaskInfo -TaskName $t.Name
        "{0}: {1} window | last run {2} | last result 0x{3:X8} | next run {4}" -f `
            $t.Name, $current, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime
        "    {0} {1}" -f $action.Execute, $action.Arguments
        continue
    }
    if ($Mode -eq "Hidden") {
        $action.Execute = "conhost.exe"
        $action.Arguments = "--headless wsl.exe $wslArgs"
    } else {
        $action.Execute = "wsl.exe"
        $action.Arguments = $wslArgs
    }
    $task.Actions = @($action)
    $task.Description = ("danger-zone {0} (user-created; docs/RUNBOOK.md). Window: {1}. " +
        "Change with ~/danger-zone/scripts/windows/task_windows.sh visible|hidden. " +
        "Log: ~/danger-zone/data/logs/.") -f $t.What, $Mode.ToLower()
    Set-ScheduledTask -InputObject $task | Out-Null
    "{0}: {1} -> {2}" -f $t.Name, $current, $Mode
}
