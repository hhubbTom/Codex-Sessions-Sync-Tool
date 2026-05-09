param(
  [switch]$InstallShortcutOnly,
  [switch]$SmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:UiScriptPath = $MyInvocation.MyCommand.Path
$script:ToolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:BackendPath = Join-Path $script:ToolRoot 'sync_backend.py'
$script:ShortcutName = 'Codex 对话同步工具.lnk'
$script:IconLocation = 'C:\Windows\System32\imageres.dll,15'
$script:BackupMap = @{}
$script:LatestState = $null
$script:ThreadMap = @{}
$script:CwdList = @()

function Get-PythonCommand {
  $candidates = New-Object System.Collections.ArrayList
  $pyCommand = Get-Command py -ErrorAction SilentlyContinue
  if ($pyCommand) {
    [void]$candidates.Add(@($pyCommand.Source, '-3'))
  }

  $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
  if ($pythonCommand) {
    [void]$candidates.Add(@($pythonCommand.Source))
  }

  $python3Command = Get-Command python3 -ErrorAction SilentlyContinue
  if ($python3Command) {
    [void]$candidates.Add(@($python3Command.Source))
  }

  foreach ($candidate in $candidates) {
    $candidateArgs = @($candidate)
    $testProcess = New-Object System.Diagnostics.Process
    $testProcess.StartInfo.FileName = $candidateArgs[0]
    if ($candidateArgs.Count -gt 1) {
      $testProcess.StartInfo.Arguments = (($candidateArgs[1..($candidateArgs.Count - 1)] + @('--version')) -join ' ')
    } else {
      $testProcess.StartInfo.Arguments = '--version'
    }
    $testProcess.StartInfo.UseShellExecute = $false
    $testProcess.StartInfo.RedirectStandardOutput = $true
    $testProcess.StartInfo.RedirectStandardError = $true
    $testProcess.StartInfo.CreateNoWindow = $true

    try {
      [void]$testProcess.Start()
      $testProcess.WaitForExit()
      if ($testProcess.ExitCode -eq 0) {
        return $candidateArgs
      }
    } catch {
      continue
    }
  }

  throw '未找到 Python。请先安装 Python 3，或确保 py/python 在 PATH 中。'
}

function ConvertTo-ProcessArgument {
  param([string]$Argument)

  if ($Argument -notmatch '[\s"]') {
    return $Argument
  }

  return '"' + ($Argument -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Invoke-Backend {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$Arguments
  )

  if (-not (Test-Path -LiteralPath $script:BackendPath)) {
    throw "缺少后端脚本: $script:BackendPath"
  }

  $pythonCommand = @(Get-PythonCommand)
  $process = New-Object System.Diagnostics.Process
  $process.StartInfo.FileName = $pythonCommand[0]
  $processArgs = New-Object System.Collections.Generic.List[string]
  for ($index = 1; $index -lt $pythonCommand.Count; $index++) {
    [void]$processArgs.Add($pythonCommand[$index])
  }
  [void]$processArgs.Add($script:BackendPath)
  foreach ($arg in $Arguments) {
    [void]$processArgs.Add($arg)
  }
  $process.StartInfo.Arguments = (($processArgs | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' ')
  $process.StartInfo.UseShellExecute = $false
  $process.StartInfo.RedirectStandardOutput = $true
  $process.StartInfo.RedirectStandardError = $true
  $process.StartInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
  $process.StartInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
  $process.StartInfo.CreateNoWindow = $true

  [void]$process.Start()
  $stdout = $process.StandardOutput.ReadToEnd()
  $stderr = $process.StandardError.ReadToEnd()
  $process.WaitForExit()
  $exitCode = $process.ExitCode
  $text = ($stdout + $stderr).Trim()
  if (-not $text) {
    throw '后端没有返回任何内容。'
  }

  try {
    $json = $text | ConvertFrom-Json
  } catch {
    throw "后端 JSON 解析失败。`r`n原始错误: $($_.Exception.Message)`r`n返回内容:`r`n$text"
  }

  if ($exitCode -ne 0 -or -not $json.ok) {
    if ($json.error) {
      throw [string]$json.error
    }
    throw "后端执行失败。`r`n$text"
  }

  return $json
}

function New-DesktopShortcut {
  $desktopPath = [Environment]::GetFolderPath('Desktop')
  $shortcutPath = Join-Path $desktopPath $script:ShortcutName
  $targetPath = Join-Path $PSHOME 'powershell.exe'
  $arguments = "-NoProfile -ExecutionPolicy Bypass -Sta -WindowStyle Hidden -File `"$script:UiScriptPath`""

  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($shortcutPath)
  $shortcut.TargetPath = $targetPath
  $shortcut.Arguments = $arguments
  $shortcut.WorkingDirectory = $script:ToolRoot
  $shortcut.IconLocation = $script:IconLocation
  $shortcut.Description = 'Codex history sync UI'
  $shortcut.Save()

  return $shortcutPath
}

if ($InstallShortcutOnly) {
  $createdShortcut = New-DesktopShortcut
  Write-Output "桌面快捷方式已创建: $createdShortcut"
  exit 0
}

function Append-Log {
  param([string]$Message)

  $timestamp = Get-Date -Format 'HH:mm:ss'
  $logBox.AppendText("[$timestamp] $Message`r`n")
  $logBox.SelectionStart = $logBox.TextLength
  $logBox.ScrollToCaret()
}

function Format-Counts {
  param($Counts)

  if (-not $Counts -or $Counts.Count -eq 0) {
    return '无'
  }

  return (($Counts | ForEach-Object { "$($_.provider)=$($_.count)" }) -join ', ')
}

function Refresh-State {
  $status = Invoke-Backend @('--json', 'status')
  $script:LatestState = $status

  $providerLabel.Text = "当前 provider: $($status.current_provider)"
  $modelLabel.Text = if ($status.current_model) { "当前模型: $($status.current_model)" } else { '当前模型: 未读取到' }
  $summaryLabel.Text = "线程总数: $($status.total_threads)    可同步到当前 provider 的线程: $($status.movable_threads)    跨设备待修复: $($status.repair_candidates)"
  $pathLabel.Text = "数据库: $($status.db_path)"

  $providersView.Items.Clear()
  foreach ($row in $status.provider_counts) {
    $isCurrent = if ($row.provider -eq $status.current_provider) { '是' } else { '' }
    $item = New-Object System.Windows.Forms.ListViewItem([string]$row.provider)
    [void]$item.SubItems.Add([string]$row.count)
    [void]$item.SubItems.Add($isCurrent)
    [void]$providersView.Items.Add($item)
  }

  $backupList.Items.Clear()
  $script:BackupMap = @{}
  foreach ($backup in $status.backups) {
    $label = "$($backup.modified_at)    $($backup.name)"
    $script:BackupMap[$label] = $backup.path
    [void]$backupList.Items.Add($label)
  }

  Append-Log "状态已刷新。当前 provider=$($status.current_provider)，可同步线程=$($status.movable_threads)，跨设备待修复=$($status.repair_candidates)。"
}

function Refresh-Threads {
  $payload = Invoke-Backend @('--json', 'list-threads', '--limit', '300')
  $threadsView.Items.Clear()
  $script:ThreadMap = @{}
  foreach ($thread in $payload.threads) {
    $script:ThreadMap[$thread.id] = $thread
    $displayTitle = if ($thread.display_title) { [string]$thread.display_title } else { [string]$thread.title }
    $item = New-Object System.Windows.Forms.ListViewItem($displayTitle)
    $item.Tag = [string]$thread.id
    [void]$item.SubItems.Add([string]$thread.cwd)
    [void]$item.SubItems.Add([string]$thread.updated_at)
    [void]$item.SubItems.Add([string]$thread.id)
    [void]$threadsView.Items.Add($item)
  }
  Append-Log "会话列表已刷新，共 $($script:ThreadMap.Count) 条。"
}

function Refresh-Cwds {
  $payload = Invoke-Backend @('--json', 'list-cwds')
  $moveCwdBox.Items.Clear()
  $publicCwd = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex'
  $options = New-Object System.Collections.Generic.List[string]
  [void]$options.Add($publicCwd)
  foreach ($row in $payload.cwds) {
    if (-not $options.Contains([string]$row.cwd)) {
      [void]$options.Add([string]$row.cwd)
    }
  }
  foreach ($cwd in $options) {
    [void]$moveCwdBox.Items.Add($cwd)
  }
  $script:CwdList = @($options)
  if (-not $moveCwdBox.Text.Trim() -and $options.Count -gt 0) {
    $moveCwdBox.Text = $options[0]
  }
  Append-Log "工作区列表已刷新，共 $($options.Count) 个。"
}

function Confirm-Action {
  param(
    [string]$Message,
    [string]$Title = '确认操作'
  )

  $choice = [System.Windows.Forms.MessageBox]::Show(
    $Message,
    $Title,
    [System.Windows.Forms.MessageBoxButtons]::OKCancel,
    [System.Windows.Forms.MessageBoxIcon]::Question
  )

  return $choice -eq [System.Windows.Forms.DialogResult]::OK
}

if ($SmokeTest) {
  $status = Invoke-Backend @('--json', 'status')
  Write-Output "Smoke test OK: provider=$($status.current_provider), threads=$($status.total_threads)"
  exit 0
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Codex 历史同步工具'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(940, 820)
$form.MinimumSize = New-Object System.Drawing.Size(940, 820)
$form.BackColor = [System.Drawing.Color]::FromArgb(247, 248, 250)
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)

$headerLabel = New-Object System.Windows.Forms.Label
$headerLabel.Text = 'Codex 历史同步工具'
$headerLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 16, [System.Drawing.FontStyle]::Bold)
$headerLabel.AutoSize = $true
$headerLabel.Location = New-Object System.Drawing.Point(20, 18)
$form.Controls.Add($headerLabel)

$warningLabel = New-Object System.Windows.Forms.Label
$warningLabel.Text = '建议先关闭 Codex Desktop 再做同步或恢复，这样最稳。'
$warningLabel.ForeColor = [System.Drawing.Color]::FromArgb(163, 64, 31)
$warningLabel.AutoSize = $true
$warningLabel.Location = New-Object System.Drawing.Point(22, 52)
$form.Controls.Add($warningLabel)

$providerLabel = New-Object System.Windows.Forms.Label
$providerLabel.Text = '当前 provider:'
$providerLabel.AutoSize = $true
$providerLabel.Location = New-Object System.Drawing.Point(22, 88)
$form.Controls.Add($providerLabel)

$modelLabel = New-Object System.Windows.Forms.Label
$modelLabel.Text = '当前模型:'
$modelLabel.AutoSize = $true
$modelLabel.Location = New-Object System.Drawing.Point(22, 112)
$form.Controls.Add($modelLabel)

$summaryLabel = New-Object System.Windows.Forms.Label
$summaryLabel.Text = '线程总数:'
$summaryLabel.AutoSize = $true
$summaryLabel.Location = New-Object System.Drawing.Point(22, 136)
$form.Controls.Add($summaryLabel)

$pathLabel = New-Object System.Windows.Forms.Label
$pathLabel.Text = '数据库:'
$pathLabel.AutoSize = $true
$pathLabel.Location = New-Object System.Drawing.Point(22, 160)
$pathLabel.MaximumSize = New-Object System.Drawing.Size(790, 0)
$form.Controls.Add($pathLabel)

$repairCwdLabel = New-Object System.Windows.Forms.Label
$repairCwdLabel.Text = '修复目标 cwd:'
$repairCwdLabel.AutoSize = $true
$repairCwdLabel.Location = New-Object System.Drawing.Point(22, 194)
$form.Controls.Add($repairCwdLabel)

$repairCwdBox = New-Object System.Windows.Forms.TextBox
$repairCwdBox.Location = New-Object System.Drawing.Point(122, 190)
$repairCwdBox.Size = New-Object System.Drawing.Size(570, 24)
$form.Controls.Add($repairCwdBox)

$repairCwdButton = New-Object System.Windows.Forms.Button
$repairCwdButton.Text = '选择文件夹'
$repairCwdButton.Size = New-Object System.Drawing.Size(110, 28)
$repairCwdButton.Location = New-Object System.Drawing.Point(708, 188)
$form.Controls.Add($repairCwdButton)

$refreshButton = New-Object System.Windows.Forms.Button
$refreshButton.Text = '刷新状态'
$refreshButton.Size = New-Object System.Drawing.Size(110, 34)
$refreshButton.Location = New-Object System.Drawing.Point(22, 232)
$form.Controls.Add($refreshButton)

$syncButton = New-Object System.Windows.Forms.Button
$syncButton.Text = '一键同步到当前'
$syncButton.Size = New-Object System.Drawing.Size(150, 34)
$syncButton.Location = New-Object System.Drawing.Point(142, 232)
$syncButton.BackColor = [System.Drawing.Color]::FromArgb(32, 91, 177)
$syncButton.ForeColor = [System.Drawing.Color]::White
$syncButton.FlatStyle = 'Flat'
$form.Controls.Add($syncButton)

$backupButton = New-Object System.Windows.Forms.Button
$backupButton.Text = '手动备份'
$backupButton.Size = New-Object System.Drawing.Size(110, 34)
$backupButton.Location = New-Object System.Drawing.Point(312, 232)
$form.Controls.Add($backupButton)

$openBackupsButton = New-Object System.Windows.Forms.Button
$openBackupsButton.Text = '打开备份目录'
$openBackupsButton.Size = New-Object System.Drawing.Size(120, 34)
$openBackupsButton.Location = New-Object System.Drawing.Point(442, 232)
$form.Controls.Add($openBackupsButton)

$shortcutButton = New-Object System.Windows.Forms.Button
$shortcutButton.Text = '重建桌面图标'
$shortcutButton.Size = New-Object System.Drawing.Size(120, 34)
$shortcutButton.Location = New-Object System.Drawing.Point(578, 232)
$form.Controls.Add($shortcutButton)

$repairButton = New-Object System.Windows.Forms.Button
$repairButton.Text = '修复导入会话'
$repairButton.Size = New-Object System.Drawing.Size(120, 34)
$repairButton.Location = New-Object System.Drawing.Point(714, 232)
$form.Controls.Add($repairButton)

$providersBox = New-Object System.Windows.Forms.GroupBox
$providersBox.Text = 'Provider 统计'
$providersBox.Location = New-Object System.Drawing.Point(22, 284)
$providersBox.Size = New-Object System.Drawing.Size(370, 170)
$form.Controls.Add($providersBox)

$providersView = New-Object System.Windows.Forms.ListView
$providersView.View = 'Details'
$providersView.FullRowSelect = $true
$providersView.GridLines = $true
$providersView.Location = New-Object System.Drawing.Point(12, 26)
$providersView.Size = New-Object System.Drawing.Size(346, 132)
[void]$providersView.Columns.Add('Provider', 170)
[void]$providersView.Columns.Add('线程数', 80)
[void]$providersView.Columns.Add('当前', 60)
$providersBox.Controls.Add($providersView)

$backupsBox = New-Object System.Windows.Forms.GroupBox
$backupsBox.Text = '备份列表'
$backupsBox.Location = New-Object System.Drawing.Point(410, 284)
$backupsBox.Size = New-Object System.Drawing.Size(410, 170)
$form.Controls.Add($backupsBox)

$backupList = New-Object System.Windows.Forms.ListBox
$backupList.Location = New-Object System.Drawing.Point(12, 24)
$backupList.Size = New-Object System.Drawing.Size(386, 94)
$backupsBox.Controls.Add($backupList)

$restoreButton = New-Object System.Windows.Forms.Button
$restoreButton.Text = '恢复选中备份'
$restoreButton.Size = New-Object System.Drawing.Size(120, 32)
$restoreButton.Location = New-Object System.Drawing.Point(12, 126)
$backupsBox.Controls.Add($restoreButton)

$restoreLatestButton = New-Object System.Windows.Forms.Button
$restoreLatestButton.Text = '恢复最新备份'
$restoreLatestButton.Size = New-Object System.Drawing.Size(120, 32)
$restoreLatestButton.Location = New-Object System.Drawing.Point(146, 126)
$backupsBox.Controls.Add($restoreLatestButton)

$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ScrollBars = 'Vertical'
$logBox.ReadOnly = $true
$logBox.Location = New-Object System.Drawing.Point(22, 640)
$logBox.Size = New-Object System.Drawing.Size(878, 128)
$logBox.BackColor = [System.Drawing.Color]::White
$form.Controls.Add($logBox)

$moveBox = New-Object System.Windows.Forms.GroupBox
$moveBox.Text = '手动归类会话'
$moveBox.Location = New-Object System.Drawing.Point(22, 470)
$moveBox.Size = New-Object System.Drawing.Size(878, 154)
$form.Controls.Add($moveBox)

$moveCwdLabel = New-Object System.Windows.Forms.Label
$moveCwdLabel.Text = '目标 cwd:'
$moveCwdLabel.AutoSize = $true
$moveCwdLabel.Location = New-Object System.Drawing.Point(12, 26)
$moveBox.Controls.Add($moveCwdLabel)

$moveCwdBox = New-Object System.Windows.Forms.ComboBox
$moveCwdBox.Location = New-Object System.Drawing.Point(82, 22)
$moveCwdBox.Size = New-Object System.Drawing.Size(450, 24)
$moveCwdBox.DropDownStyle = 'DropDown'
$moveBox.Controls.Add($moveCwdBox)

$publicCwdButton = New-Object System.Windows.Forms.Button
$publicCwdButton.Text = '公共空间'
$publicCwdButton.Size = New-Object System.Drawing.Size(90, 28)
$publicCwdButton.Location = New-Object System.Drawing.Point(544, 20)
$moveBox.Controls.Add($publicCwdButton)

$moveCwdButton = New-Object System.Windows.Forms.Button
$moveCwdButton.Text = '选择文件夹'
$moveCwdButton.Size = New-Object System.Drawing.Size(100, 28)
$moveCwdButton.Location = New-Object System.Drawing.Point(644, 20)
$moveBox.Controls.Add($moveCwdButton)

$moveThreadButton = New-Object System.Windows.Forms.Button
$moveThreadButton.Text = '移动选中会话'
$moveThreadButton.Size = New-Object System.Drawing.Size(110, 28)
$moveThreadButton.Location = New-Object System.Drawing.Point(754, 20)
$moveBox.Controls.Add($moveThreadButton)

$refreshThreadsButton = New-Object System.Windows.Forms.Button
$refreshThreadsButton.Text = '刷新'
$refreshThreadsButton.Size = New-Object System.Drawing.Size(54, 28)
$refreshThreadsButton.Location = New-Object System.Drawing.Point(814, 20)
$moveBox.Controls.Add($refreshThreadsButton)

$threadsView = New-Object System.Windows.Forms.ListView
$threadsView.View = 'Details'
$threadsView.FullRowSelect = $true
$threadsView.GridLines = $true
$threadsView.Location = New-Object System.Drawing.Point(12, 58)
$threadsView.Size = New-Object System.Drawing.Size(854, 84)
[void]$threadsView.Columns.Add('标题', 210)
[void]$threadsView.Columns.Add('当前 cwd', 360)
[void]$threadsView.Columns.Add('更新时间', 90)
[void]$threadsView.Columns.Add('Thread ID', 180)
$moveBox.Controls.Add($threadsView)

$refreshButton.Add_Click({
  try {
    Refresh-State
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '刷新失败', 'OK', 'Error') | Out-Null
    Append-Log "刷新失败: $($_.Exception.Message)"
  }
})

$syncButton.Add_Click({
  try {
    if (-not $script:LatestState) {
      Refresh-State
    }
    if ([int]$script:LatestState.movable_threads -le 0) {
      [System.Windows.Forms.MessageBox]::Show('当前已经全部归到正在使用的 provider 下面了。', '无需同步', 'OK', 'Information') | Out-Null
      Append-Log '同步跳过：没有需要迁移的线程。'
      return
    }
    $message = "将会把其他 provider 的线程统一归到当前 provider:`r`n$($script:LatestState.current_provider)`r`n`r`n本次预计移动线程数: $($script:LatestState.movable_threads)`r`n每次都会先自动备份数据库。"
    if (-not (Confirm-Action -Message $message -Title '确认同步')) {
      Append-Log '用户取消了同步。'
      return
    }

    $result = Invoke-Backend @('--json', 'sync')
    Append-Log "同步完成。已移动 $($result.updated_rows) 条线程。"
    Append-Log "同步前: $(Format-Counts $result.before_counts)"
    Append-Log "同步后: $(Format-Counts $result.after_counts)"
    Append-Log "备份文件: $($result.backup_path)"
    Refresh-State
    [System.Windows.Forms.MessageBox]::Show('同步完成。若左侧历史列表没有立刻刷新，重开一次 Codex 即可。', '同步完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '同步失败', 'OK', 'Error') | Out-Null
    Append-Log "同步失败: $($_.Exception.Message)"
  }
})

$backupButton.Add_Click({
  try {
    $result = Invoke-Backend @('--json', 'backup')
    Append-Log "手动备份完成: $($result.backup_path)"
    Refresh-State
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '备份失败', 'OK', 'Error') | Out-Null
    Append-Log "备份失败: $($_.Exception.Message)"
  }
})

$openBackupsButton.Add_Click({
  try {
    if (-not $script:LatestState) {
      Refresh-State
    }
    $folder = $script:LatestState.backup_dir
    if (-not (Test-Path -LiteralPath $folder)) {
      New-Item -ItemType Directory -Force -Path $folder | Out-Null
    }
    Start-Process explorer.exe $folder
    Append-Log "已打开备份目录: $folder"
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '打开目录失败', 'OK', 'Error') | Out-Null
    Append-Log "打开备份目录失败: $($_.Exception.Message)"
  }
})

$shortcutButton.Add_Click({
  try {
    $path = New-DesktopShortcut
    Append-Log "桌面快捷方式已更新: $path"
    [System.Windows.Forms.MessageBox]::Show("桌面快捷方式已更新：`r`n$path", '完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '创建快捷方式失败', 'OK', 'Error') | Out-Null
    Append-Log "创建快捷方式失败: $($_.Exception.Message)"
  }
})

$repairCwdButton.Add_Click({
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = '选择修复后归属的项目文件夹'
  $dialog.ShowNewFolderButton = $true
  if ($repairCwdBox.Text.Trim()) {
    $dialog.SelectedPath = $repairCwdBox.Text.Trim()
  }
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $repairCwdBox.Text = $dialog.SelectedPath
    Append-Log "修复目标 cwd 已设置为: $($dialog.SelectedPath)"
  }
})

$repairButton.Add_Click({
  try {
    if (-not $script:LatestState) {
      Refresh-State
    }
    if ([int]$script:LatestState.repair_candidates -le 0) {
      [System.Windows.Forms.MessageBox]::Show('当前没有检测到需要跨设备修复的导入会话。', '无需修复', 'OK', 'Information') | Out-Null
      Append-Log '修复跳过：没有检测到跨设备导入会话。'
      return
    }
    $message = "将会修复从其他设备复制过来的会话记录。`r`n`r`n这会同时更新 jsonl 会话头部和本地线程数据库，并先自动备份。`r`n如果填写了修复目标 cwd，会话会归到该项目文件夹；未填写时才使用当前设备最近在用的本地路径。"
    if (-not (Confirm-Action -Message $message -Title '确认修复导入会话')) {
      Append-Log '用户取消了修复导入会话。'
      return
    }

    $repairArgs = New-Object System.Collections.Generic.List[string]
    [void]$repairArgs.Add('--json')
    [void]$repairArgs.Add('repair')
    if ($repairCwdBox.Text.Trim()) {
      [void]$repairArgs.Add('--cwd')
      [void]$repairArgs.Add($repairCwdBox.Text.Trim())
    }
    $result = Invoke-Backend $repairArgs.ToArray()
    $repairedCount = if ($result.repaired_threads) { $result.repaired_threads.Count } else { 0 }
    $skippedCount = if ($result.skipped_threads) { $result.skipped_threads.Count } else { 0 }
    Append-Log "导入会话修复完成。已修复 $repairedCount 条，跳过 $skippedCount 条。"
    Append-Log "目标 cwd: $($result.target_cwd)"
    Append-Log "数据库备份: $($result.db_backup)"
    if ($result.session_backup_dir) {
      Append-Log "会话文件备份目录: $($result.session_backup_dir)"
    }
    Refresh-State
    [System.Windows.Forms.MessageBox]::Show('修复完成。请重开一次 Codex Desktop 再看左侧历史列表。', '修复完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '修复失败', 'OK', 'Error') | Out-Null
    Append-Log "修复失败: $($_.Exception.Message)"
  }
})

$publicCwdButton.Add_Click({
  $publicCwd = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex'
  $moveCwdBox.Text = $publicCwd
  Append-Log "移动目标 cwd 已设置为公共空间: $publicCwd"
})

$moveCwdButton.Add_Click({
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = '选择会话移动后的项目文件夹'
  $dialog.ShowNewFolderButton = $true
  if ($moveCwdBox.Text.Trim()) {
    $dialog.SelectedPath = $moveCwdBox.Text.Trim()
  }
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $moveCwdBox.Text = $dialog.SelectedPath
    Append-Log "移动目标 cwd 已设置为: $($dialog.SelectedPath)"
  }
})

$refreshThreadsButton.Add_Click({
  try {
    Refresh-Cwds
    Refresh-Threads
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '刷新会话失败', 'OK', 'Error') | Out-Null
    Append-Log "刷新会话失败: $($_.Exception.Message)"
  }
})

$moveThreadButton.Add_Click({
  try {
    if ($threadsView.SelectedItems.Count -eq 0) {
      [System.Windows.Forms.MessageBox]::Show('先在手动归类列表里选一个会话。', '未选择会话', 'OK', 'Warning') | Out-Null
      return
    }
    $targetCwd = $moveCwdBox.Text.Trim()
    if (-not $targetCwd) {
      [System.Windows.Forms.MessageBox]::Show('请先填写或选择目标 cwd。', '缺少目标 cwd', 'OK', 'Warning') | Out-Null
      return
    }

    $selectedItem = $threadsView.SelectedItems[0]
    $threadId = [string]$selectedItem.Tag
    $thread = $script:ThreadMap[$threadId]
    $message = "将移动会话：`r`n$($thread.title)`r`n`r`n当前 cwd:`r`n$($thread.cwd)`r`n`r`n目标 cwd:`r`n$targetCwd`r`n`r`n会先自动备份数据库和 jsonl。"
    if (-not (Confirm-Action -Message $message -Title '确认移动会话')) {
      Append-Log '用户取消了移动会话。'
      return
    }

    $result = Invoke-Backend @('--json', 'move-thread', '--thread-id', $threadId, '--cwd', $targetCwd)
    $movedCount = if ($result.moved_threads) { $result.moved_threads.Count } else { 0 }
    $skippedCount = if ($result.skipped_threads) { $result.skipped_threads.Count } else { 0 }
    Append-Log "会话移动完成。已移动 $movedCount 条，跳过 $skippedCount 条。"
    Append-Log "目标 cwd: $($result.target_cwd)"
    Append-Log "数据库备份: $($result.db_backup)"
    if ($result.session_backup_dir) {
      Append-Log "会话文件备份目录: $($result.session_backup_dir)"
    }
    Refresh-State
    Refresh-Threads
    [System.Windows.Forms.MessageBox]::Show('移动完成。请重开一次 Codex Desktop 再看左侧历史列表。', '移动完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '移动失败', 'OK', 'Error') | Out-Null
    Append-Log "移动失败: $($_.Exception.Message)"
  }
})

$restoreButton.Add_Click({
  try {
    if ($backupList.SelectedItem -eq $null) {
      [System.Windows.Forms.MessageBox]::Show('先在右侧选一个备份。', '未选择备份', 'OK', 'Warning') | Out-Null
      return
    }
    $selectedLabel = [string]$backupList.SelectedItem
    $backupPath = $script:BackupMap[$selectedLabel]
    if (-not $backupPath) {
      throw '无法解析选中的备份路径。'
    }

    $message = "将会恢复这个备份：`r`n$backupPath`r`n`r`n恢复前会再自动生成一份安全备份。"
    if (-not (Confirm-Action -Message $message -Title '确认恢复')) {
      Append-Log '用户取消了恢复。'
      return
    }

    $result = Invoke-Backend @('--json', 'restore', '--backup', $backupPath)
    Append-Log "恢复完成。来源备份: $($result.restored_from)"
    Append-Log "恢复前安全备份: $($result.safety_backup)"
    Refresh-State
    [System.Windows.Forms.MessageBox]::Show('恢复完成。建议重开一次 Codex 再看历史列表。', '恢复完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '恢复失败', 'OK', 'Error') | Out-Null
    Append-Log "恢复失败: $($_.Exception.Message)"
  }
})

$restoreLatestButton.Add_Click({
  try {
    if (-not (Confirm-Action -Message '将会恢复最新备份，并在恢复前再做一次安全备份。' -Title '确认恢复最新备份')) {
      Append-Log '用户取消了恢复最新备份。'
      return
    }

    $result = Invoke-Backend @('--json', 'restore')
    Append-Log "已恢复最新备份: $($result.restored_from)"
    Append-Log "恢复前安全备份: $($result.safety_backup)"
    Refresh-State
    [System.Windows.Forms.MessageBox]::Show('恢复完成。建议重开一次 Codex 再看历史列表。', '恢复完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '恢复失败', 'OK', 'Error') | Out-Null
    Append-Log "恢复失败: $($_.Exception.Message)"
  }
})

try {
  $createdShortcut = New-DesktopShortcut
  Append-Log "桌面快捷方式已准备好: $createdShortcut"
} catch {
  Append-Log "初始化快捷方式失败: $($_.Exception.Message)"
}

try {
  Refresh-State
  Refresh-Cwds
  Refresh-Threads
} catch {
  Append-Log "初始化状态失败: $($_.Exception.Message)"
  [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '启动失败', 'OK', 'Error') | Out-Null
}

[void]$form.ShowDialog()
