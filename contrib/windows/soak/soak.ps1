param(
  [int]$Minutes = 18,
  [int]$Rows = 20000,
  [string]$Printer = "CUPS PDF",
  [string]$ShareDrive = "Z:",
  [string]$EdgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
)
$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$share = "${ShareDrive}\soak-$stamp"
$local = "C:\vemu\soak\$stamp"
New-Item -ItemType Directory -Force -Path $share,$local | Out-Null
$log = Join-Path $share 'soak.log'
$localLog = Join-Path $local 'soak.log'
$done = 'C:\vemu\soak\soak.done'
$latest = 'C:\vemu\soak\latest-summary.json'
Remove-Item -Force -ErrorAction SilentlyContinue $done,$latest

function Log([string]$msg) {
  $line = "{0:HH:mm:ss} {1}" -f (Get-Date), $msg
  Add-Content -Path $log -Value $line -ErrorAction SilentlyContinue
  Add-Content -Path $localLog -Value $line -ErrorAction SilentlyContinue
}

Log "START stamp=$stamp minutes=$Minutes rows=$Rows printer=$Printer share=$ShareDrive host=$env:COMPUTERNAME"
try {
  $p = Get-CimInstance Win32_Printer -ErrorAction Stop | Where-Object { $_.Name -eq $Printer } | Select-Object -First 1
  if ($null -eq $p) { throw "Printer not found: $Printer" }
  Invoke-CimMethod -InputObject $p -MethodName SetDefaultPrinter | Out-Null
  Log "default printer $Printer"
} catch { Log "printer default error: $($_.Exception.Message)" }

Copy-Item 'C:\vemu\soak\stress.html' (Join-Path $local 'stress.html') -Force
$edgePath = $EdgePath
try {
  Start-Process $edgePath -ArgumentList '--new-window',"file:///C:/vemu/soak/$stamp/stress.html"
  Log 'edge started'
} catch { Log "edge error: $($_.Exception.Message)" }

$rows = $Rows
$cols = 8
$excel = $null
$wb = $null
$ws = $null
$word = $null
$doc = $null
$ppt = $null
$pres = $null

try {
  $excel = New-Object -ComObject Excel.Application
  $excel.Visible = $true
  $excel.DisplayAlerts = $false
  $wb = $excel.Workbooks.Add()
  $ws = $wb.Worksheets.Item(1)
  $ws.Name = 'Soak'
  $data = New-Object 'object[,]' $rows,$cols
  for ($r = 0; $r -lt $rows; $r++) {
    for ($c = 0; $c -lt $cols; $c++) {
      if ($c -eq 0) { $data[$r,$c] = "SOAK-$stamp" }
      elseif ($c -eq 1) { $data[$r,$c] = $r + 1 }
      else { $data[$r,$c] = [math]::Round((Get-Random -Minimum 1 -Maximum 1000) + $r * 0.01, 2) }
    }
  }
  $ws.Range($ws.Cells.Item(1,1), $ws.Cells.Item($rows,$cols)).Value2 = $data
  $ws.Cells.Item(1,10).Formula = "=SUM(B1:B$rows)"
  $ws.Cells.Item(2,10).Formula = "=AVERAGE(C1:C$rows)"
  $ws.Columns.Item(1).ColumnWidth = 22
  $wb.SaveAs((Join-Path $share 'soak.xlsx'), 51)
  Log "excel filled $rows rows"
} catch { Log "excel setup error: $($_.Exception.Message)" }

try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $true
  $word.DisplayAlerts = 0
  $word.Options.SaveInterval = 0
  $doc = $word.Documents.Add()
  $word.Activate()
  $doc.Activate()
  $sel = $word.Selection
  if ($null -eq $sel) { $sel = $doc.ActiveWindow.Selection }
  $sel.TypeText("vEMU soak $stamp`r`n")
  $sel.TypeParagraph()
  if ($ws) {
    $ws.Range('A1:H50').Copy() | Out-Null
    $sel.PasteExcelTable($false,$false,$false)
    $sel.TypeParagraph()
    $text = $doc.Content.Text
    if ($text -match [regex]::Escape("SOAK-$stamp")) {
      Log 'excel-to-word paste verified'
    } else {
      Log 'excel-to-word paste marker missing'
    }
  }
  $doc.SaveAs([ref](Join-Path $share 'soak.docx'))
  Log 'word created'
} catch { Log "word setup error: $($_.Exception.Message)" }

$start = Get-Date
$duration = [TimeSpan]::FromMinutes($Minutes)
$i = 0
$printCount = 0
while (((Get-Date) - $start) -lt $duration) {
  $i++
  $excelOk = $false
  for ($attempt = 1; $attempt -le 3 -and -not $excelOk; $attempt++) {
    try {
      if ($ws) {
        for ($k = 0; $k -lt 200; $k++) {
          $r = Get-Random -Minimum 2 -Maximum ($rows + 1)
          $ws.Cells.Item($r,3).Value2 = [math]::Round((Get-Random -Minimum 1 -Maximum 1000),2)
        }
        $excel.Calculate()
      }
      $excelOk = $true
    } catch {
      Log "RETRY excel ${i}/${attempt}: $($_.Exception.Message)"
      Start-Sleep -Milliseconds 800
    }
  }
  $wordOk = $false
  for ($attempt = 1; $attempt -le 6 -and -not $wordOk; $attempt++) {
    try {
      if ($ws -and $word -and $doc) {
        Start-Sleep -Milliseconds 400
        $ws.Range('A1:H50').Copy() | Out-Null
        $word.Activate()
        $doc.Activate()
        $sel = $word.Selection
        if ($null -eq $sel) { $sel = $doc.ActiveWindow.Selection }
        if ($null -eq $sel) { throw 'Word selection unavailable' }
        $sel.EndKey(6) | Out-Null
        $sel.TypeParagraph()
        $sel.PasteExcelTable($false,$false,$false)
        $sel.TypeParagraph()
        $sel.TypeText("Iteration $i at $(Get-Date -Format HH:mm:ss)")
        $sel.TypeParagraph()
        Start-Sleep -Milliseconds 300
        $doc.Save()
      }
      $wordOk = $true
    } catch {
      Log "RETRY word ${i}/${attempt}: $($_.Exception.Message)"
      Start-Sleep -Seconds 2
    }
  }
  if (-not $wordOk) { Log "ERR word paste ${i}: gave up" }
  $clip = ''
  try {
    Set-Clipboard -Value "vemu-soak-$i-$stamp"
    $clip = Get-Clipboard
    if ($clip -ne "vemu-soak-$i-$stamp") { Log "ERR clipboard mismatch ${i}: $clip" }
  } catch { Log "ERR clipboard ${i}: $($_.Exception.Message)" }
  if (($i % 3) -eq 0) {
    try {
      "vEMU soak print test $stamp iteration $i $(Get-Date -Format o)" | Out-Printer -Name $Printer
      $printCount++
      Log "print job sent $i"
    } catch { Log "ERR out-printer ${i}: $($_.Exception.Message)" }
  }
  Log "iter $i clip=$clip"
  Start-Sleep -Seconds 30
}

$summary = [pscustomobject]@{
  stamp = $stamp
  minutes = $Minutes
  iterations = $i
  print_jobs = $printCount
  elapsed_min = [math]::Round(((Get-Date) - $start).TotalMinutes,1)
  share = $share
  local = $local
  excel_rows = $rows
  word_pages = if ($doc) { $doc.ComputeStatistics(2) } else { 0 }
  edge_processes = @(Get-Process msedge -ErrorAction SilentlyContinue).Count
  excel_processes = @(Get-Process EXCEL -ErrorAction SilentlyContinue).Count
  word_processes = @(Get-Process WINWORD -ErrorAction SilentlyContinue).Count
  free_c = (Get-PSDrive C).Free
}
$json = $summary | ConvertTo-Json
$json | Set-Content (Join-Path $share 'summary.json')
$json | Set-Content (Join-Path $local 'summary.json')
$json | Set-Content $latest
Log "SUMMARY $json"
Set-Content -Path $done -Value $stamp
try { if ($wb) { $wb.Save(); $wb.Close($false) } } catch {}
try { if ($doc) { $doc.Save(); $doc.Close($false) } } catch {}
try { if ($pres) { $pres.Save(); $pres.Close() } } catch {}
try { if ($excel) { $excel.Quit() } } catch {}
try { if ($word) { $word.Quit() } } catch {}
try { if ($ppt) { $ppt.Quit() } } catch {}
Log 'DONE'
