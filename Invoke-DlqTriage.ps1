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
$ReasonLookup = @{
    2001 = @{ Code="MQRC_ALIAS_APPL_TYPE_ERROR"; Class="Configuration"; Desc="Alias application type error." }
    2033 = @{ Code="MQRC_NO_MSG_AVAILABLE";      Class="Transient";     Desc="No message available on queue." }
    2035 = @{ Code="MQRC_NOT_AUTHORIZED";        Class="Configuration"; Desc="Security authority check failed. User not authorized." }
    2052 = @{ Code="MQRC_Q_DELETED";             Class="Configuration"; Desc="Target queue was deleted." }
    2053 = @{ Code="MQRC_Q_FULL";                Class="Transient";     Desc="Target queue reached MAXDEPTH. Temporary capacity issue." }
    2082 = @{ Code="MQRC_UNKNOWN_ALIAS_BASE_Q";  Class="Configuration"; Desc="Unknown alias base queue." }
    2085 = @{ Code="MQRC_UNKNOWN_OBJECT_NAME";   Class="Configuration"; Desc="Target destination queue does not exist on Queue Manager." }
    2152 = @{ Code="MQRC_WRONG_MD_VERSION";      Class="Data";          Desc="Invalid MQMD version or corrupt message header." }
    2153 = @{ Code="MQRC_WRONG_MD_FORMAT";       Class="Data";          Desc="Invalid MQMD format structure." }
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

# Parse MQDLH structure from binary payload
function Parse-MQDLH {
    param([byte[]]$Bytes)
    if ($Bytes.Length -lt 172) { return $null }

    $strucId = [System.Text.Encoding]::ASCII.GetString($Bytes, 0, 4)
    if ($strucId -ne "DLH ") { return $null }

    $reason = [BitConverter]::ToInt32($Bytes, 8)
    $destQ  = [System.Text.Encoding]::ASCII.GetString($Bytes, 12, 48).Trim()
    $destQM = [System.Text.Encoding]::ASCII.GetString($Bytes, 60, 48).Trim()
    $format = [System.Text.Encoding]::ASCII.GetString($Bytes, 116, 8).Trim()
    $putAppl= [System.Text.Encoding]::ASCII.GetString($Bytes, 128, 28).Trim()
    $putDate= [System.Text.Encoding]::ASCII.GetString($Bytes, 156, 8).Trim()
    $putTime= [System.Text.Encoding]::ASCII.GetString($Bytes, 164, 8).Trim()

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

    # Open DLQ for Browsing or Getting
    $openOptions = [IBM.WMQ.MQC]::MQOO_BROWSE -bor [IBM.WMQ.MQC]::MQOO_INQUIRE -bor [IBM.WMQ.MQC]::MQOO_FAIL_IF_QUIESCING
    if ($Replay -and -not $DryRun) {
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
                            $targetQ = $qMgr.AccessQueue($dlh.DestQName, [IBM.WMQ.MQC]::MQOO_OUTPUT -bor [IBM.WMQ.MQC]::MQOO_FAIL_IF_QUIESCING)
                            $replayMsg = [IBM.WMQ.MQMessage]::new()
                            $replayMsg.Format = $dlh.Format
                            $replayMsg.WriteBytes($dlh.PayloadBytes)
                            $pmo = [IBM.WMQ.MQPutMessageOptions]::new()
                            $targetQ.Put($replayMsg, $pmo)
                            $targetQ.Close()
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
