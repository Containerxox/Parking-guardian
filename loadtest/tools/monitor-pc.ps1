# 부하 발생기(이 PC) 감시: k6 프로세스 CPU, 전체 CPU, 여유 메모리, 네트워크 송수신을 N초마다 CSV 로 남긴다.
# 목적(사전 등록 3절 무효 판정): "서버가 느린가, 테스트 PC 가 못 따라가는가"를 구분한다.
#   - k6 CPU >= 70% (전체 코어 대비) 이면 그 회차는 무효.
#   - 전체 CPU 가 계속 90% 이상이거나 메모리가 부족해도 의심한다.
#
# 사용: k6 를 시작하기 직전에 다른 터미널에서 실행하고, k6 가 끝나면 Ctrl+C 로 멈춘다.
#   powershell -File loadtest\tools\monitor-pc.ps1 -Out <결과폴더>\pc-monitor.csv [-IntervalSec 5] [-DurationSec 400]
# 읽기 전용이다. 네트워크로 아무것도 보내지 않는다.
param(
  [Parameter(Mandatory = $true)][string]$Out,
  [int]$IntervalSec = 5,
  [int]$DurationSec = 0   # 0 이면 Ctrl+C 까지
)
$ErrorActionPreference = 'Stop'
$cores = [Environment]::ProcessorCount
# 인터넷으로 나가는 기본 경로(0.0.0.0/0)의 어댑터를 쓴다. 속도가 높은 어댑터가 아니라 실제로 트래픽이 지나가는 어댑터여야 한다.
$route = Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object { $_.RouteMetric + (Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4).InterfaceMetric } | Select-Object -First 1
$nic = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex
if (-not $nic) { throw '활성 네트워크 어댑터를 찾지 못했습니다.' }
"# adapter=$($nic.Name) type=$($nic.PhysicalMediaType) link=$($nic.LinkSpeed) cores=$cores" | Out-File -Encoding utf8 $Out
'time_utc,k6_cpu_pct_of_all_cores,total_cpu_pct,free_mem_mb,net_rx_kbps,net_tx_kbps,k6_running' | Out-File -Encoding utf8 -Append $Out

function Get-K6Cpu { $p = Get-Process -Name k6 -ErrorAction SilentlyContinue; if ($p) { ($p | Measure-Object CPU -Sum).Sum } else { $null } }
$prevStat = Get-NetAdapterStatistics -Name $nic.Name
$prevCpu = Get-K6Cpu
$start = Get-Date
while ($true) {
  Start-Sleep -Seconds $IntervalSec
  $stat = Get-NetAdapterStatistics -Name $nic.Name
  $cpu = Get-K6Cpu
  $k6pct = ''
  if ($null -ne $cpu -and $null -ne $prevCpu) { $k6pct = [math]::Round((($cpu - $prevCpu) / $IntervalSec) / $cores * 100, 1) }
  $total = [math]::Round((Get-Counter '\Processor(_Total)\% Processor Time').CounterSamples[0].CookedValue, 1)
  $mem = [math]::Round((Get-Counter '\Memory\Available MBytes').CounterSamples[0].CookedValue, 0)
  $rx = [math]::Round(($stat.ReceivedBytes - $prevStat.ReceivedBytes) * 8 / 1000 / $IntervalSec, 0)
  $tx = [math]::Round(($stat.SentBytes - $prevStat.SentBytes) * 8 / 1000 / $IntervalSec, 0)
  $line = '{0},{1},{2},{3},{4},{5},{6}' -f (Get-Date).ToUniversalTime().ToString('s'), $k6pct, $total, $mem, $rx, $tx, [int]($null -ne $cpu)
  $line | Out-File -Encoding utf8 -Append $Out
  $prevStat = $stat; $prevCpu = $cpu
  if ($DurationSec -gt 0 -and ((Get-Date) - $start).TotalSeconds -ge $DurationSec) { break }
}
