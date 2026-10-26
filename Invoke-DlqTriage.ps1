#Requires -Version 5.1
<#
.SYNOPSIS
    Automated IBM MQ & ACE Dead Letter Queue (DLQ) Triage and Resolution Tool
.DESCRIPTION
    Executes an 8-step triage lifecycle:
    1. Detection       - Check DLQ queue depth & detect failed messages.
    2. Identification  - Extract target queue & queue manager from MQDLH.
    3. Identification  - Parse MQMD / MQDLH metadata (MsgId, CorrelId, timestamps).
    4. Inspection      - Inspect and display payload safely.
    5. Analysis        - Resolve MQDLH reason code to human-readable explanation.
    6. Analysis        - Correlate with ACE (App Connect Enterprise) event logs.
    7. Analysis        - Classify failure type (Transient, Data, Configuration, Poison).
    8. Decision        - Check retry safety and optionally replay back to target queue.
#>

[CmdletBinding()]
param(
    [string]$EnvFile = "",
    [string]$TargetQMgr = "",
    [string]$HostNameParam = "",
    [int]$PortParam = 0,
    [string]$ChannelParam = "",
    [string]$DlqParam = "",
    [string]$UserParam = "",
    [string]$PasswordParam = "",
    [string]$Action = "Triage", # "Triage", "TestConnection", "Replay"
    [string]$TargetMsgId = "",
    [switch]$Replay,
    [switch]$ForceReplay,
    [switch]$DryRun = $true,
    [int]$MaxMessages = 50,
    [switch]$AsJson
)

if (-not $EnvFile) {
    if ($PSScriptRoot) {
        $EnvFile = Join-Path $PSScriptRoot ".env"
    } else {
        $EnvFile = Join-Path (Get-Location) ".env"
    }
}

# Load configuration from .env file
function Get-EnvConfig {
    param([string]$Path)
    $config = @{}
    if (Test-Path $Path) {
        Get-Content $Path | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $idx = $line.IndexOf("=")
                $key = $line.Substring(0, $idx).Trim()
                $val = $line.Substring($idx + 1).Trim()
                $config[$key] = $val
            }
        }
    }
    return $config
}

$cfg = Get-EnvConfig -Path $EnvFile

$QMgrName = if ($TargetQMgr)     { $TargetQMgr }     elseif ($cfg["MQ_QMGR"])     { $cfg["MQ_QMGR"] }     else { "QMGR" }
$HostName = if ($HostNameParam)  { $HostNameParam }  elseif ($cfg["MQ_HOST"])     { $cfg["MQ_HOST"] }     else { "localhost" }
$Port     = if ($PortParam -gt 0){ $PortParam }      elseif ($cfg["MQ_PORT"])     { [int]$cfg["MQ_PORT"] } else { 1414 }
$Channel  = if ($ChannelParam)   { $ChannelParam }   elseif ($cfg["MQ_CHANNEL"])  { $cfg["MQ_CHANNEL"] }  else { "SYSTEM.DEF.SVRCONN" }
$DlqName  = if ($DlqParam)       { $DlqParam }       elseif ($cfg["MQ_DLQ_NAME"]) { $cfg["MQ_DLQ_NAME"] } else { "SYSTEM.DEAD.LETTER.QUEUE" }
$UserId   = if ($UserParam)      { $UserParam }      else { $cfg["MQ_USER"] }
$Password = if ($PasswordParam)  { $PasswordParam }  else { $cfg["MQ_PASSWORD"] }

if (-not $AsJson) {
    Write-Host "====================================================================" -ForegroundColor Cyan
    Write-Host " IBM MQ & ACE - DLQ Resolution & Triage Utility" -ForegroundColor Cyan
    Write-Host "====================================================================" -ForegroundColor Cyan
    Write-Host "Queue Manager: $QMgrName"
    Write-Host "Host:          $HostName : $Port"
    Write-Host "Channel:       $Channel"
    Write-Host "DLQ Name:      $DlqName"
    Write-Host "User ID:       $(if ($UserId) { $UserId } else { '[None specified]' })"
}

# Load IBM MQ .NET Client Assembly
$mqDll = "C:\Program Files\IBM\MQ\bin\amqmdnet.dll"
if (-not (Test-Path $mqDll)) {
    if ($AsJson) {
        @{ success = $false; error = "IBM MQ .NET client library not found at $mqDll." } | ConvertTo-Json
    } else {
        Write-Error "IBM MQ .NET client library not found at $mqDll."
    }
    exit 1
}
Add-Type -Path $mqDll -ErrorAction SilentlyContinue

# MQ Reason Code Reference Dictionary
# Covers the most common reason codes seen in Dead Letter Queues.
$ReasonLookup = @{
    # Transient / Recoverable
    2009 = @{ Code="MQRC_CONNECTION_BROKEN";      Class="Transient";     Desc="Network connection to the Queue Manager was lost mid-operation." }
    2033 = @{ Code="MQRC_NO_MSG_AVAILABLE";       Class="Transient";     Desc="No message available on queue at the time of the GET call." }
    2053 = @{ Code="MQRC_Q_FULL";                 Class="Transient";     Desc="Target queue reached MAXDEPTH. Temporary capacity issue - retry after queue drains." }
    2059 = @{ Code="MQRC_Q_MGR_NOT_AVAILABLE";    Class="Transient";     Desc="Queue Manager is not running or its TCP listener is stopped." }
    2162 = @{ Code="MQRC_Q_MGR_QUIESCING";        Class="Transient";     Desc="Queue Manager is shutting down gracefully. Retry later." }
    2195 = @{ Code="MQRC_UNEXPECTED_ERROR";       Class="Transient";     Desc="Unexpected internal MQ error. Check queue manager error logs (AMQERR01.LOG)." }
    2537 = @{ Code="MQRC_CHANNEL_NOT_AVAILABLE";  Class="Transient";     Desc="SVRCONN channel is currently unavailable (max instances reached or stopped)." }

    # Configuration Errors
    2001 = @{ Code="MQRC_ALIAS_APPL_TYPE_ERROR";  Class="Configuration"; Desc="Alias application type error - check alias queue definition." }
    2014 = @{ Code="MQRC_BACKED_OUT";             Class="Configuration"; Desc="Message has been backed out. Backout count may have exceeded the threshold." }
    2035 = @{ Code="MQRC_NOT_AUTHORIZED";         Class="Configuration"; Desc="Security authority check failed. User not authorized for the target queue or queue manager." }
    2052 = @{ Code="MQRC_Q_DELETED";              Class="Configuration"; Desc="Target queue was deleted after the message was put. Re-create the queue." }
    2058 = @{ Code="MQRC_Q_MGR_NAME_ERROR";       Class="Configuration"; Desc="Queue Manager name does not match the port Queue Manager. Check port assignments." }
    2063 = @{ Code="MQRC_SECURITY_ERROR";         Class="Configuration"; Desc="MQ security error - domain/LDAP lookup failed. Set MCAUSER to MUSR_MQADMIN on the SVRCONN channel." }
    2082 = @{ Code="MQRC_UNKNOWN_ALIAS_BASE_Q";   Class="Configuration"; Desc="Unknown alias base queue - the queue this alias resolves to does not exist." }
    2085 = @{ Code="MQRC_UNKNOWN_OBJECT_NAME";    Class="Configuration"; Desc="Target destination queue does not exist on the Queue Manager. Create the missing queue." }
    2087 = @{ Code="MQRC_UNKNOWN_REMOTE_Q_MGR";   Class="Configuration"; Desc="Remote Queue Manager name is unknown. Check transmission queue and remote queue definitions." }
    2100 = @{ Code="MQRC_XMIT_Q_WRONG_TYPE";     Class="Configuration"; Desc="Transmission queue is the wrong type. Must be a local queue." }
    2102 = @{ Code="MQRC_NO_XMIT_Q";             Class="Configuration"; Desc="No transmission queue defined for the remote Queue Manager." }
    2540 = @{ Code="MQRC_UNKNOWN_CHANNEL_NAME";   Class="Configuration"; Desc="SVRCONN channel name not found on the Queue Manager. Verify channel name spelling." }

    # Data / Schema Errors
    2080 = @{ Code="MQRC_TRUNCATED_MSG_FAILED";   Class="Data";          Desc="Message was truncated during GET - buffer too small or message corrupted." }
    2110 = @{ Code="MQRC_FORMAT_ERROR";           Class="Data";          Desc="Message format does not match the format name in the MQMD. Check application encoding." }
    2150 = @{ Code="MQRC_WRONG_FORMAT";           Class="Data";          Desc="Message body format is inconsistent with the MQMD Format field." }
    2152 = @{ Code="MQRC_WRONG_MD_VERSION";       Class="Data";          Desc="Invalid MQMD version or corrupt message descriptor header." }
    2153 = @{ Code="MQRC_WRONG_MD_FORMAT";        Class="Data";          Desc="Invalid MQMD format structure - message may be corrupt." }

    # Poison Message
    2016 = @{ Code="MQRC_GET_INHIBITED";          Class="Poison";        Desc="GET inhibited on the target queue - queue is intentionally blocked from consuming." }
}

# Correlate with ACE Event Log
function Search-AceLogs {
    param([string]$MsgId, [string]$CorrelId, [datetime]$Timestamp)
    $startTime = $Timestamp.AddMinutes(-10)
    $endTime   = $Timestamp.AddMinutes(10)

    try {
        $events = Get-WinEvent -FilterHashtable @{
            LogName   = 'Application'
            StartTime = $startTime
            EndTime   = $endTime
        } -ErrorAction SilentlyContinue | Where-Object {
            $_.ProviderName -match "App Connect|WebSphere Broker|IBM Integration" -or
            $_.Message -match $MsgId -or
            ($CorrelId -and $_.Message -match $CorrelId)
        } | Select-Object -First 3

        return $events
    } catch {
        return @()
    }
}

# Parse MQDLH structure from binary payload.
# The MQDLH is exactly 172 bytes in version 1 (the only version IBM MQ places on the DLQ).
# Layout reference: IBM MQ Knowledge Center - MQDLH structure.
function Parse-MQDLH {
    param([byte[]]$Bytes)
    if ($Bytes.Length -lt 172) { return $null }

    # Verify StrucId = "DLH " (4 bytes, ASCII)
    $strucId = [System.Text.Encoding]::ASCII.GetString($Bytes, 0, 4)
    if ($strucId -ne "DLH ") { return $null }

    # Version field at offset 4 (4-byte int). IBM MQ only writes version 1.
    $version = [BitConverter]::ToInt32($Bytes, 4)
    if ($version -ne 1) {
        Write-Warning "Parse-MQDLH: Unexpected MQDLH version $version - parsing may be inaccurate."
    }

    # Reason at offset 8 (4-byte int).
    $reason = [BitConverter]::ToInt32($Bytes, 8)

    # MQDLH field layout (corrected offsets):
    #   DestQName    : offset  12, 48 bytes
    #   DestQMgrName : offset  60, 48 bytes
    #   Encoding     : offset 108,  4 bytes (int)
    #   CodedCharSetId: offset 112,  4 bytes (int)
    #   Format       : offset 116,  8 bytes
    #   Flags        : offset 124,  4 bytes (int)
    #   PutApplType  : offset 128,  4 bytes (int)
    #   PutApplName  : offset 132, 28 bytes
    #   PutDate      : offset 160,  8 bytes
    #   PutTime      : offset 168,  4 bytes  => total = 172
    $destQ   = [System.Text.Encoding]::ASCII.GetString($Bytes,  12, 48).TrimEnd([char]0, ' ')
    $destQM  = [System.Text.Encoding]::ASCII.GetString($Bytes,  60, 48).TrimEnd([char]0, ' ')
    $format  = [System.Text.Encoding]::ASCII.GetString($Bytes, 116,  8).TrimEnd([char]0, ' ')
    $putAppl = [System.Text.Encoding]::ASCII.GetString($Bytes, 132, 28).TrimEnd([char]0, ' ')
    $putDate = [System.Text.Encoding]::ASCII.GetString($Bytes, 160,  8).TrimEnd([char]0, ' ')
    $putTime = [System.Text.Encoding]::ASCII.GetString($Bytes, 168,  4).TrimEnd([char]0, ' ')

    $payloadBytes = [byte[]]::new($Bytes.Length - 172)
    [Array]::Copy($Bytes, 172, $payloadBytes, 0, $payloadBytes.Length)

    return @{
        Reason       = $reason
        DestQName    = $destQ
        DestQMgrName = $destQM
        Format       = $format
        PutApplName  = $putAppl
        PutDate      = $putDate
        PutTime      = $putTime
        PayloadBytes = $payloadBytes
    }
}

# Connection Setup
$connProps = @{
    [IBM.WMQ.MQC]::TRANSPORT_PROPERTY = [IBM.WMQ.MQC]::TRANSPORT_MQSERIES_CLIENT
    [IBM.WMQ.MQC]::HOST_NAME_PROPERTY = $HostName
    [IBM.WMQ.MQC]::PORT_PROPERTY      = $Port
    [IBM.WMQ.MQC]::CHANNEL_PROPERTY   = $Channel
}

if ($UserId)   { $connProps[[IBM.WMQ.MQC]::USER_ID_PROPERTY]  = $UserId }
if ($Password) { $connProps[[IBM.WMQ.MQC]::PASSWORD_PROPERTY] = $Password }

try {
    $qMgr = [IBM.WMQ.MQQueueManager]::new($QMgrName, $connProps)
    if (-not $AsJson) {
        Write-Host "[SUCCESS] Connected to $QMgrName." -ForegroundColor Green
    }
} catch {
    $errMsg = $_.Exception.Message
    if ($_.Exception.InnerException) { $errMsg += " - " + $_.Exception.InnerException.Message }
    if ($AsJson) {
        @{ success = $false; error = $errMsg } | ConvertTo-Json
    } else {
        Write-Host "[ERROR] Failed to connect: $errMsg" -ForegroundColor Red
    }
    exit 1
}

try {
    # If action is simply TestConnection:
    if ($Action -eq "TestConnection") {
        $openOptions = [IBM.WMQ.MQC]::MQOO_INQUIRE -bor [IBM.WMQ.MQC]::MQOO_FAIL_IF_QUIESCING
        $testQ = $qMgr.AccessQueue($DlqName, $openOptions)
        $curDepth = $testQ.CurrentDepth
        $testQ.Close()
        $qMgr.Disconnect()
        
        $res = @{
            success  = $true
            qmgr     = $QMgrName
            host     = $HostName
            port     = $Port
            channel  = $Channel
            dlq      = $DlqName
            curDepth = $curDepth
        }
        if ($AsJson) { $res | ConvertTo-Json } else { Write-Host "Test connection successful! Queue depth: $curDepth" }
        exit 0
    }

    # Open DLQ for Browsing or Destructive-Get.
    # MQOO_INPUT_SHARED is required when the Replay action will perform a real
    # (non-dry-run) destructive GET to remove the message from the DLQ after
    # re-putting it to the target queue.  The -Replay switch and the -Action
    # "Replay" API path must both be treated identically here.
    $isLiveReplay = (($Replay -or ($Action -eq "Replay")) -and (-not $DryRun))
    $openOptions  = [IBM.WMQ.MQC]::MQOO_BROWSE -bor [IBM.WMQ.MQC]::MQOO_INQUIRE -bor [IBM.WMQ.MQC]::MQOO_FAIL_IF_QUIESCING
    if ($isLiveReplay) {
        $openOptions = $openOptions -bor [IBM.WMQ.MQC]::MQOO_INPUT_SHARED
    }
    $dlq = $qMgr.AccessQueue($DlqName, $openOptions)
    
    $curDepth = $dlq.CurrentDepth
    if (-not $AsJson) {
        Write-Host "`n[STEP 1 - Detection] DLQ Current Depth: $curDepth message(s)" -ForegroundColor Cyan
    }
    
    $messagesResult = @()

    if ($curDepth -gt 0) {
        $gmo = [IBM.WMQ.MQGetMessageOptions]::new()
        $gmo.Options = [IBM.WMQ.MQC]::MQGMO_BROWSE_FIRST -bor [IBM.WMQ.MQC]::MQGMO_NO_WAIT -bor [IBM.WMQ.MQC]::MQGMO_FAIL_IF_QUIESCING

        $processed = 0
        while ($processed -lt $MaxMessages) {
            $msg = [IBM.WMQ.MQMessage]::new()
            try {
                $dlq.Get($msg, $gmo)
            } catch [IBM.WMQ.MQException] {
                if ($_.Exception.Reason -eq [IBM.WMQ.MQC]::MQRC_NO_MSG_AVAILABLE) { break }
                throw
            }

            $processed++
            $msgBytes = $msg.ReadBytes($msg.MessageLength)
            $msgIdHex = ($msg.MessageId | ForEach-Object { $_.ToString("X2") }) -join ""
            $correlIdHex = ($msg.CorrelationId | ForEach-Object { $_.ToString("X2") }) -join ""
            $putDateTime = $msg.PutDateTime

            $dlh = Parse-MQDLH -Bytes $msgBytes
            
            $msgInfo = [ordered]@{
                index        = $processed
                msgId        = $msgIdHex
                correlId     = $correlIdHex
                putDateTime  = $putDateTime.ToString("yyyy-MM-dd HH:mm:ss")
                backoutCount = $msg.BackoutCount
                format       = $msg.Format.Trim()
                hasDlh       = ($null -ne $dlh)
            }

            if ($dlh) {
                $reasonCode = $dlh.Reason
                $reasonInfo = $ReasonLookup[$reasonCode]
                $failureClass = if ($reasonInfo) { $reasonInfo.Class } else { "Unknown" }
                $reasonDesc   = if ($reasonInfo) { $reasonInfo.Desc } else { "MQ reason $reasonCode" }
                $reasonName   = if ($reasonInfo) { $reasonInfo.Code } else { "MQRC_$reasonCode" }

                $payloadText = [System.Text.Encoding]::UTF8.GetString($dlh.PayloadBytes)
                $previewLen  = [Math]::Min(500, $payloadText.Length)

                $aceEvents = Search-AceLogs -MsgId $msgIdHex -CorrelId $correlIdHex -Timestamp $putDateTime
                $aceSummary = @()
                foreach ($ev in $aceEvents) {
                    $aceSummary += @{
                        id      = $ev.Id
                        time    = $ev.TimeCreated.ToString("yyyy-MM-dd HH:mm:ss")
                        message = $ev.Message
                    }
                }

                $isSafeToRetry = (($failureClass -eq "Transient") -or $ForceReplay) -and ($msg.BackoutCount -lt 5)

                $msgInfo["targetQueue"]   = $dlh.DestQName
                $msgInfo["targetQMgr"]    = $dlh.DestQMgrName
                $msgInfo["originalApp"]   = $dlh.PutApplName
                $msgInfo["reasonCode"]    = $reasonCode
                $msgInfo["reasonName"]    = $reasonName
                $msgInfo["reasonDesc"]    = $reasonDesc
                $msgInfo["failureClass"]  = $failureClass
                $msgInfo["payloadText"]   = $payloadText.Substring(0, $previewLen)
                $msgInfo["payloadLength"] = $payloadText.Length
                $msgInfo["aceEvents"]     = $aceSummary
                $msgInfo["isSafeToRetry"] = $isSafeToRetry

                if (-not $AsJson) {
                    Write-Host "`n------------------------------------------------------------" -ForegroundColor DarkGray
                    Write-Host "Message #$processed Triage Details" -ForegroundColor White
                    Write-Host "  MsgId:        $msgIdHex"
                    Write-Host "  Target Queue: $($dlh.DestQName)"
                    Write-Host "  Reason:       $reasonCode ($reasonName)" -ForegroundColor Red
                    Write-Host "  Class:        $failureClass" -ForegroundColor Cyan
                    Write-Host "  Safe Retry:   $isSafeToRetry"
                }

                # Replay Logic if requested
                $shouldReplayThis = ($Replay -or ($Action -eq "Replay")) -and ($isSafeToRetry -or $ForceReplay)
                if ($TargetMsgId -and ($msgIdHex -ne $TargetMsgId)) {
                    $shouldReplayThis = $false
                }

                if ($shouldReplayThis) {
                    if ($DryRun) {
                        $msgInfo["replayStatus"] = "DRY-RUN: Would replay to '$($dlh.DestQName)'"
                        if (-not $AsJson) { Write-Host "  [DRY-RUN] Would re-put stripped payload into '$($dlh.DestQName)'." -ForegroundColor Cyan }
                    } else {
                        try {
                            # PUT the stripped payload (MQDLH removed) to the original target queue.
                            $targetQ   = $qMgr.AccessQueue($dlh.DestQName, [IBM.WMQ.MQC]::MQOO_OUTPUT -bor [IBM.WMQ.MQC]::MQOO_FAIL_IF_QUIESCING)
                            $replayMsg = [IBM.WMQ.MQMessage]::new()
                            $replayMsg.Format = $dlh.Format
                            $replayMsg.WriteBytes($dlh.PayloadBytes)
                            $pmo = [IBM.WMQ.MQPutMessageOptions]::new()
                            $targetQ.Put($replayMsg, $pmo)
                            $targetQ.Close()

                            # Now destructively remove the message from the DLQ by its MsgId.
                            # The DLQ was opened with MQOO_INPUT_SHARED for exactly this purpose.
                            $delGmo = [IBM.WMQ.MQGetMessageOptions]::new()
                            $delGmo.Options  = [IBM.WMQ.MQC]::MQGMO_MSG_UNDER_CURSOR -bor [IBM.WMQ.MQC]::MQGMO_NO_WAIT -bor [IBM.WMQ.MQC]::MQGMO_FAIL_IF_QUIESCING
                            $delMsg = [IBM.WMQ.MQMessage]::new()
                            try {
                                $dlq.Get($delMsg, $delGmo)
                            } catch {
                                # Non-fatal: message was replayed but may remain on DLQ if destructive
                                # GET fails. Log the issue but keep replayStatus as success.
                                if (-not $AsJson) {
                                    Write-Host "  [WARN] Could not remove replayed message from DLQ: $($_.Exception.Message)" -ForegroundColor Yellow
                                }
                            }

                            $msgInfo["replayStatus"] = "REPLAYED: Put successfully to '$($dlh.DestQName)'"
                            if (-not $AsJson) { Write-Host "  [REPLAY SUCCESS] Replayed to '$($dlh.DestQName)'." -ForegroundColor Green }
                        } catch {
                            $msgInfo["replayStatus"] = "FAILED: $($_.Exception.Message)"
                            if (-not $AsJson) { Write-Host "  [REPLAY FAILED] $($_.Exception.Message)" -ForegroundColor Red }
                        }
                    }
                }
            }

            $messagesResult += $msgInfo

            # Advance browse cursor to next message
            $gmo.Options = [IBM.WMQ.MQC]::MQGMO_BROWSE_NEXT -bor [IBM.WMQ.MQC]::MQGMO_NO_WAIT -bor [IBM.WMQ.MQC]::MQGMO_FAIL_IF_QUIESCING
        }
    }

    $dlq.Close()
    $qMgr.Disconnect()

    if ($AsJson) {
        $finalOutput = [ordered]@{
            success    = $true
            qmgr       = $QMgrName
            host       = $HostName
            port       = $Port
            channel    = $Channel
            dlq        = $DlqName
            curDepth   = $curDepth
            count      = $messagesResult.Count
            messages   = $messagesResult
        }
        $finalOutput | ConvertTo-Json -Depth 6
    } else {
        Write-Host "`n[COMPLETED] Triage finished for $processed message(s)." -ForegroundColor Green
    }

} catch {
    $errMsg = $_.Exception.Message
    if ($_.Exception.InnerException) { $errMsg += " - " + $_.Exception.InnerException.Message }
    if ($AsJson) {
        @{ success = $false; error = $errMsg } | ConvertTo-Json
    } else {
        Write-Host "[ERROR] Execution failure: $errMsg" -ForegroundColor Red
    }
    if ($qMgr -and $qMgr.IsConnected) { $qMgr.Disconnect() }
}
