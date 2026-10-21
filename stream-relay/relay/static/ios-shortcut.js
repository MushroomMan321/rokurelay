// Paste into an iOS Shortcut "Run JavaScript on Web Page" action.
// It runs inside the Safari page you shared, so it sees the stream URLs the page's own
// player already loaded (with their tokens), plus this browser's cookies and User-Agent.
// The next action ("Get Contents of URL", POST, Request Body: File) sends this JSON to
// http://<relay>:8080/api/cast.
var seen = {}, candidates = [];
function add(u) {
  if (!u || !/^https?:/i.test(u) || seen[u]) return;
  if (!/\.m3u8|\.mpd|format=m3u8|mpegurl|\.mp4(\?|$)/i.test(u)) return;
  seen[u] = 1;
  candidates.push(u);
}
performance.getEntriesByType("resource").forEach(function (e) { add(e.name); });
document.querySelectorAll("video, source").forEach(function (v) { add(v.currentSrc || v.src); });
var frames = Array.prototype.map.call(document.querySelectorAll("iframe"), function (f) { return f.src; })
  .filter(function (s) { return /^https?:/.test(s); }).slice(0, 4);

completion(JSON.stringify({
  url: location.href,
  title: document.title,
  candidates: candidates.slice(0, 6),
  frames: frames,
  client_user_agent: navigator.userAgent,
  cookies: document.cookie || undefined
  // token: "your RELAY_TOKEN", // uncomment if you set RELAY_TOKEN
}));
