' RokuRelay - entry point.
' The relay launches us over ECP with ?contentId=<session>&relay=<url>; when we're
' already running it sends the same params as an roInputEvent instead.

sub Main(args as Dynamic)
    screen = CreateObject("roSGScreen")
    port = CreateObject("roMessagePort")
    screen.SetMessagePort(port)
    scene = screen.CreateScene("MainScene")
    screen.Show()

    input = CreateObject("roInput")
    input.SetMessagePort(port)

    scene.observeField("exitApp", port)
    scene.relayUrl = ResolveRelayUrl(args)
    if args <> invalid and args.contentId <> invalid then scene.deepLink = args

    while true
        msg = wait(0, port)
        msgType = type(msg)
        if msgType = "roSGScreenEvent"
            if msg.IsScreenClosed() then return
        else if msgType = "roInputEvent"
            info = msg.GetInfo()
            if info <> invalid
                if info.relay <> invalid and info.relay <> "" then scene.relayUrl = SaveRelayUrl(info.relay)
                scene.deepLink = info
            end if
        else if msgType = "roSGNodeEvent"
            if msg.GetField() = "exitApp" and msg.GetData() = true then return
        end if
    end while
end sub

' Launch param > saved registry value > manifest default.
function ResolveRelayUrl(args as Dynamic) as String
    if args <> invalid and args.relay <> invalid and args.relay <> "" then return SaveRelayUrl(args.relay)
    section = CreateObject("roRegistrySection", "relay")
    if section.Exists("url") then return section.Read("url")
    return CreateObject("roAppInfo").GetValue("relay_url")
end function

function SaveRelayUrl(url as String) as String
    clean = url
    while Len(clean) > 0 and Right(clean, 1) = "/"
        clean = Left(clean, Len(clean) - 1)
    end while
    section = CreateObject("roRegistrySection", "relay")
    if section.Read("url") <> clean
        section.Write("url", clean)
        section.Flush()
    end if
    return clean
end function
