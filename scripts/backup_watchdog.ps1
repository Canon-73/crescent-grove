# backup_watchdog.ps1
# 3時のDドライブ自動バックアップが止まっていないか監視する番犬。
#
# 背景: 2026-06-21〜08-08、scheduler の env var 化で .env の追記漏れがあり、
# バックアップが「skip ログだけ出して」7週間サイレントに止まっていた。
# CGサーバ側にも通知機構を入れたが、サーバ本体が死んでいる・コードにバグがある
# ケースでは自己申告できないため、サーバから完全に独立したこのスクリプトを
# Windows タスクスケジューラ（タスク名: CrescentGrove_BackupWatchdog）で毎日 12:30 に実行する。
#
# 判定: 以下のうち新しい方が閾値（既定33時間）より古ければ警告ポップアップを出す。
#   - 鮮度スタンプ D:\agent_backup\last_backup_ok.txt（バックアップ成功時にサーバが書く）
#   - ミラー内 data\ フォルダの最新ファイル更新時刻（柚月の data は毎日必ず変わるため、
#     ミラーが新鮮なら必ず新しい。スタンプ機構が無い旧コードでも検知できる保険）
# 33時間 = 毎日3時実行なら12:30時点で約9.5時間のはずなので、1回でも欠けたら翌日に発報する。
#
# 追加: サーバが書く問題ファイル logs\backup_problem.txt（GDrive失敗・設定不備など、
# ミラーの鮮度では見えない異常）が33時間以内に更新されていれば、その内容も警告する。
# ※バックアップ異常を柚月経由で通知するのは廃止（睡眠中の柚月を起こした事故 2026-08-13）。
#   カノンへの通知はこのポップアップ一本に集約されている。
#
# 通知の見た目（2026-09-02）: 以前は MessageBox 一枚だったが、他のウィンドウの裏に埋もれて
# 気付けなかったため、最前面固定・画面中央の大きな赤いウィンドウに変更した。音は鳴らさない。
#
# 手動テスト:
#   powershell -File scripts\backup_watchdog.ps1 -NoUi                     # 正常なら OK と出る
#   powershell -File scripts\backup_watchdog.ps1 -NoUi -ThresholdHours 0.01  # 警告文の確認
#   powershell -File scripts\backup_watchdog.ps1 -ThresholdHours 0.01 -AutoCloseSeconds 5  # ウィンドウの見た目確認

param(
    [double]$ThresholdHours = 33,
    [switch]$NoUi,
    [int]$AutoCloseSeconds = 0    # テスト用。0 より大きければその秒数で自動的に閉じる
)

$mirror  = 'D:\agent_backup\agent'
$stamp   = 'D:\agent_backup\last_backup_ok.txt'
$problem = 'C:\Users\canon\デスクトップ\agent\logs\backup_problem.txt'

# スタンプと data\ 内最新ファイルのうち、新しい方をバックアップの鮮度とみなす
$freshest = $null
if (Test-Path $stamp) {
    $freshest = (Get-Item $stamp).LastWriteTime
}
$dataDir = Join-Path $mirror 'data'
if (Test-Path $dataDir) {
    $newest = Get-ChildItem -Path $dataDir -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest -and ($null -eq $freshest -or $newest.LastWriteTime -gt $freshest)) {
        $freshest = $newest.LastWriteTime
    }
}

# サーバが記録した問題（GDrive失敗・設定不備など。鮮度では見えない異常）
$problemMsg = $null
if (Test-Path $problem) {
    $pAge = (Get-Date) - (Get-Item $problem).LastWriteTime
    if ($pAge.TotalHours -lt $ThresholdHours) {
        $lines = Get-Content $problem -Encoding UTF8 | Select-Object -Last 3
        $problemMsg = "サーバがバックアップの異常を記録しています:`n" + ($lines -join "`n")
    }
}

$staleMsg = $null
if ($null -ne $freshest) {
    $age = (Get-Date) - $freshest
    if ($age.TotalHours -ge $ThresholdHours) {
        $staleMsg = ("Dドライブのバックアップが {0:N1} 時間更新されていません（最終: {1}）。`n" -f $age.TotalHours, $freshest) +
                    "3時の自動バックアップが止まっている可能性があります。"
    }
} else {
    $staleMsg = "Dドライブのバックアップ先 ($mirror) が見つかりません。`nバックアップが動いていない可能性があります。"
}

if ($null -eq $staleMsg -and $null -eq $problemMsg) {
    if ($NoUi) { Write-Output ("OK: 最終バックアップ {0}（{1:N1}時間前）・問題記録なし" -f $freshest, $age.TotalHours) }
    exit 0
}

$msg = (@($staleMsg, $problemMsg) | Where-Object { $_ }) -join "`n`n"
$msg += "`n`nClaude Code に調査を依頼してください。"

if ($NoUi) {
    Write-Output "ALERT: $msg"
    exit 1
}
# 最前面固定・画面中央の大きな赤いウィンドウで知らせる（音は鳴らさない）
Add-Type -AssemblyName System.Windows.Forms | Out-Null
Add-Type -AssemblyName System.Drawing | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form = New-Object System.Windows.Forms.Form
$form.Text            = 'Crescent Grove バックアップ警告'
$form.TopMost         = $true
$form.StartPosition   = 'CenterScreen'
$form.Size            = New-Object System.Drawing.Size([int]($screen.Width * 0.6), [int]($screen.Height * 0.6))
$form.MinimumSize     = New-Object System.Drawing.Size(720, 480)
$form.BackColor       = [System.Drawing.Color]::FromArgb(176, 24, 24)
$form.ForeColor       = [System.Drawing.Color]::White
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox     = $false
$form.MinimizeBox     = $false
$form.ShowInTaskbar   = $true

$title = New-Object System.Windows.Forms.Label
$title.Text      = 'バックアップが止まっています'
$title.Font      = New-Object System.Drawing.Font('Yu Gothic UI', 32, [System.Drawing.FontStyle]::Bold)
$title.AutoSize  = $false
$title.TextAlign = 'MiddleCenter'
$title.Dock      = 'Top'
$title.Height    = 110

$body = New-Object System.Windows.Forms.TextBox
$body.Text        = $msg
$body.Multiline   = $true
$body.ReadOnly    = $true
$body.BorderStyle = 'None'
$body.Font        = New-Object System.Drawing.Font('Yu Gothic UI', 16)
$body.BackColor   = $form.BackColor
$body.ForeColor   = [System.Drawing.Color]::White
$body.Dock        = 'Fill'
$body.TabStop     = $false

# TextBox の Dock=Fill に余白を付けるため、Padding 付きのパネルで包む
$bodyWrap = New-Object System.Windows.Forms.Panel
$bodyWrap.Dock    = 'Fill'
$bodyWrap.Padding = New-Object System.Windows.Forms.Padding(60, 10, 60, 10)
$bodyWrap.Controls.Add($body)

$footer = New-Object System.Windows.Forms.Panel
$footer.Dock   = 'Bottom'
$footer.Height = 110

$btn = New-Object System.Windows.Forms.Button
$btn.Text      = '確認した'
$btn.Font      = New-Object System.Drawing.Font('Yu Gothic UI', 18, [System.Drawing.FontStyle]::Bold)
$btn.Size      = New-Object System.Drawing.Size(280, 64)
$btn.FlatStyle = 'Flat'
$btn.BackColor = [System.Drawing.Color]::White
$btn.ForeColor = $form.BackColor
$btn.FlatAppearance.BorderSize = 0
$btn.Add_Click({ $form.Close() })
$footer.Add_Resize({ $btn.Location = New-Object System.Drawing.Point([int](($footer.Width - $btn.Width) / 2), [int](($footer.Height - $btn.Height) / 2)) })
$footer.Controls.Add($btn)

# Dock の重なり順: 後から Add したものが外側になるので Fill を先に入れる
$form.Controls.Add($bodyWrap)
$form.Controls.Add($footer)
$form.Controls.Add($title)
$form.AcceptButton = $btn

if ($AutoCloseSeconds -gt 0) {
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = $AutoCloseSeconds * 1000
    $timer.Add_Tick({ $timer.Stop(); $form.Close() })
    $timer.Start()
}

# 起動直後に確実に前へ出す（スケジューラから起動されるとフォーカスが来ないことがある）
$form.Add_Shown({ $form.Activate(); $btn.Focus() })
[void]$form.ShowDialog()
exit 1
