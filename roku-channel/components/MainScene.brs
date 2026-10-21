' Single full-screen player that follows the relay:
'  - polls /api/status and plays whatever session is "ready"
'  - reloads on errors with backoff, and when the picture freezes or buffering hangs
'  - after repeated failures asks the relay to re-resolve the link (/api/heal)

sub init()
    m.video = m.top.findNode("video")
    m.overlay = m.top.findNode("overlay")
    m.titleLabel = m.top.findNode("titleLabel")
    m.statusLabel = m.top.findNode("statusLabel")
    m.retryTimer = m.top.findNode("retryTimer")
    m.watchdog = m.top.findNode("watchdog")
    m.overlayTimer = m.top.findNode("overlayTimer")

    m.poller = invalid
    m.status = invalid      ' last status from the relay
    m.sid = ""              ' session loaded in the player
    m.gen = -1              ' generation loaded in the player
    m.shownErrorSid = ""
    m.retries = 0
    m.lastPos = -1
    m.stuckTicks = 0
    m.bufferTicks = 0
    m.playingTicks = 0

    m.video.observeField("state", "onVideoState")
    m.retryTimer.observeField("fire", "onRetryTimer")
    m.watchdog.observeField("fire", "onWatchdog")
    m.overlayTimer.observeField("fire", "onOverlayTimer")
    m.watchdog.control = "start"

    m.top.setFocus(true)
    showOverlay("RokuRelay", "Connecting to the relay...", false)
end sub

' ---------------------------------------------------------------- relay polling

sub onRelayUrlChanged()
    url = m.top.relayUrl
    if url = invalid or url = ""
        showOverlay("RokuRelay", "No relay address set. Cast a link from your phone to configure this TV.", false)
        return
    end if
    if m.poller <> invalid then m.poller.control = "stop"
    m.poller = CreateObject("roSGNode", "StatusTask")
    m.poller.relayUrl = url
    m.poller.observeField("status", "onStatus")
    m.poller.observeField("reachable", "onReachable")
    m.poller.control = "run"
end sub

sub onDeepLink()
    ' The relay just pushed a new cast; don't wait for the next poll.
    if m.poller <> invalid then m.poller.pollNow = true
end sub

sub onReachable()
    if m.poller.reachable
        if m.sid = "" then onStatus()
    else if m.sid = ""
        showOverlay("RokuRelay", "Can't reach the relay at " + m.top.relayUrl + ". Is the container running?", false)
    end if
end sub

sub onStatus()
    if m.poller = invalid then return
    s = m.poller.status
    if s = invalid or s.state = invalid then return
    m.status = s
    sid = asString(s.session_id)
    gen = asInt(s.generation)
    title = asString(s.title)

    if s.state = "ready"
        if sid <> m.sid or gen <> m.gen then playStream(s, true)
    else if s.state = "resolving"
        if sid <> m.sid or m.sid = ""
            stopVideo()
            showOverlay(title, "Finding the stream...", false)
        end if
    else if s.state = "error"
        if sid <> m.shownErrorSid
            m.shownErrorSid = sid
            stopVideo()
            showOverlay(title, "Couldn't play this: " + asString(s.error), false)
        end if
    else
        if m.sid <> "" then stopVideo()
        showOverlay("RokuRelay", "Nothing playing. Cast a link from your phone.", false)
    end if
end sub

' ---------------------------------------------------------------- playback

sub playStream(s as Object, fresh as Boolean)
    content = CreateObject("roSGNode", "ContentNode")
    content.url = s.stream_url
    content.streamFormat = s.format
    content.title = asString(s.title)
    content.live = asBool(s.live)

    m.retryTimer.control = "stop"
    m.video.control = "stop"
    m.video.content = content
    m.video.control = "play"
    m.video.setFocus(true)

    m.sid = asString(s.session_id)
    m.gen = asInt(s.generation)
    if fresh then m.retries = 0
    resetWatchdog()
    if fresh then showOverlay(asString(s.title), "Starting...", false)
end sub

sub stopVideo()
    m.retryTimer.control = "stop"
    m.video.control = "stop"
    m.sid = ""
    m.gen = -1
    resetWatchdog()
    m.top.setFocus(true)
end sub

sub onVideoState()
    state = m.video.state
    if state = "playing"
        m.bufferTicks = 0
        hideOverlaySoon()
    else if state = "error"
        scheduleRetry("Playback error: " + asString(m.video.errorMsg))
    else if state = "finished"
        if m.status <> invalid and asBool(m.status.live)
            scheduleRetry("Stream ended unexpectedly")
        else
            showOverlay(currentTitle(), "Finished. Cast another link from your phone.", false)
        end if
    end if
end sub

sub scheduleRetry(reason as String)
    if m.status = invalid or m.status.state <> "ready" then return
    m.retries = m.retries + 1
    ' 2, 4, 8, then every 15 seconds
    delay = 2
    for i = 2 to m.retries
        delay = delay * 2
    end for
    if delay > 15 then delay = 15
    if m.retries >= 3 and m.poller <> invalid then m.poller.heal = true
    m.retryTimer.duration = delay
    m.retryTimer.control = "start"
    showOverlay(currentTitle(), reason + Chr(10) + "Reconnecting in " + delay.ToStr() + "s (attempt " + m.retries.ToStr() + ")", false)
end sub

sub onRetryTimer()
    if m.status <> invalid and m.status.state = "ready" then playStream(m.status, false)
end sub

sub reloadNow(reason as String)
    m.video.control = "stop"
    scheduleRetry(reason)
end sub

' Every 5s: catch frozen video and endless buffering that never raise an error.
sub onWatchdog()
    if m.sid = "" then return
    state = m.video.state
    if state = "playing"
        position = m.video.position
        if position = m.lastPos
            m.stuckTicks = m.stuckTicks + 1
        else
            m.stuckTicks = 0
            m.playingTicks = m.playingTicks + 1
        end if
        m.lastPos = position
        if m.playingTicks >= 6 then m.retries = 0   ' 30s of clean playback
        if m.stuckTicks >= 3 then reloadNow("The picture froze")
    else if state = "buffering"
        m.bufferTicks = m.bufferTicks + 1
        if m.bufferTicks >= 5 then reloadNow("Stuck buffering")
    end if
end sub

sub resetWatchdog()
    m.lastPos = -1
    m.stuckTicks = 0
    m.bufferTicks = 0
    m.playingTicks = 0
end sub

' ---------------------------------------------------------------- overlay + keys

sub showOverlay(title as String, message as String, autoHide as Boolean)
    m.titleLabel.text = title
    m.statusLabel.text = message
    m.overlay.visible = true
    m.overlayTimer.control = "stop"
    if autoHide then m.overlayTimer.control = "start"
end sub

sub hideOverlaySoon()
    m.statusLabel.text = "Playing"
    m.overlayTimer.control = "stop"
    m.overlayTimer.control = "start"
end sub

sub onOverlayTimer()
    if m.video.state = "playing" then m.overlay.visible = false
end sub

function currentTitle() as String
    if m.status <> invalid then return asString(m.status.title)
    return "RokuRelay"
end function

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false
    if key = "up"
        if m.overlay.visible and m.video.state = "playing"
            m.overlay.visible = false
        else
            msg = "Relay: " + asString(m.top.relayUrl) + "   State: " + m.video.state
            if m.status <> invalid then msg = msg + " / " + asString(m.status.state)
            showOverlay(currentTitle(), msg, m.video.state = "playing")
        end if
        return true
    else if key = "options"
        ' * button: get a fresh link from the relay and restart playback
        if m.poller <> invalid then m.poller.heal = true
        if m.status <> invalid and m.status.state = "ready"
            showOverlay(currentTitle(), "Refreshing the stream...", false)
            playStream(m.status, true)
        end if
        return true
    else if key = "back"
        m.top.exitApp = true
        return true
    end if
    return false
end function

' ---------------------------------------------------------------- helpers

function asString(v as Dynamic) as String
    if v = invalid then return ""
    t = type(v)
    if t = "roString" or t = "String" then return v
    if t = "roInt" or t = "Integer" or t = "roFloat" or t = "Float" or t = "Double" or t = "roDouble" or t = "LongInteger" then return v.ToStr()
    if t = "roBoolean" or t = "Boolean"
        if v then return "true"
        return "false"
    end if
    return ""
end function

' Comparing invalid to a Boolean is a runtime crash in BrightScript, so normalise first.
function asBool(v as Dynamic) as Boolean
    if v = invalid then return false
    t = type(v)
    if t = "roBoolean" or t = "Boolean" then return v
    return false
end function

function asInt(v as Dynamic) as Integer
    if v = invalid then return 0
    t = type(v)
    if t = "roInt" or t = "Integer" or t = "LongInteger" then return v
    if t = "roFloat" or t = "Float" or t = "Double" or t = "roDouble" then return Int(v)
    if t = "roString" or t = "String" then return v.ToInt()
    return 0
end function
