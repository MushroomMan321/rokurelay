sub init()
    m.top.functionName = "pollLoop"
end sub

sub pollLoop()
    port = CreateObject("roMessagePort")
    m.top.observeField("pollNow", port)
    m.top.observeField("heal", port)
    base = m.top.relayUrl
    lastBody = ""
    failures = 0

    while true
        body = httpRequest("GET", base + "/api/status?compact=1", 4000)
        if body = invalid
            failures = failures + 1
            ' a single dropped poll isn't worth alarming anyone about
            if failures >= 2 and m.top.reachable then m.top.reachable = false
        else
            failures = 0
            if not m.top.reachable then m.top.reachable = true
            if body <> lastBody
                parsed = ParseJson(body)
                if parsed <> invalid
                    lastBody = body
                    m.top.status = parsed
                end if
            end if
        end if

        msg = wait(m.top.interval * 1000, port)
        if type(msg) = "roSGNodeEvent" and msg.GetField() = "heal"
            ' Ask the relay to re-resolve the link (fresh tokens / new ffmpeg).
            httpRequest("POST", base + "/api/heal", 30000)
        end if
    end while
end sub

function httpRequest(method as String, url as String, timeoutMs as Integer) as Dynamic
    xfer = CreateObject("roUrlTransfer")
    port = CreateObject("roMessagePort")
    xfer.SetMessagePort(port)
    xfer.SetUrl(url)
    xfer.AddHeader("Accept", "application/json")
    xfer.EnableEncodings(true)
    if method = "POST"
        xfer.AddHeader("Content-Type", "application/json")
        started = xfer.AsyncPostFromString("{}")
    else
        started = xfer.AsyncGetToString()
    end if
    if not started then return invalid

    msg = wait(timeoutMs, port)
    if type(msg) = "roUrlEvent"
        if msg.GetResponseCode() = 200 then return msg.GetString()
        return invalid
    end if
    xfer.AsyncCancel()
    return invalid
end function
