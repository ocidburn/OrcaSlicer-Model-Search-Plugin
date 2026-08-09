#!/usr/bin/env python3
"""Standalone WebKitGTK harness reproducing the OrcaSlicer plugin webview.

Same libwebkit2gtk-4.1 Orca links, same env vars the orca-belt launcher exports,
same load path (load_html + file:// base URI). Runs on Xvfb so xdotool clicks are
reliable and the user's desktop is untouched.
"""
import json
import os
import sys
import subprocess

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules["orca"] = None  # block the GNOME screen-reader module of the same name
from search_engine import PAGE  # noqa: E402

MODELS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")))
BASE_URI = "file:///home/tommaso/projects/orca/orcaslicer-pr/belt-2026-08-local/resources/web/"

# Mirrors ORCA_BRIDGE_JS in PluginWebDialog.cpp, but routes to a probe handler.
BRIDGE = """
(function () {
  if (window.orca) return;
  var handlers = [];
  window.orca = {
    postMessage: function (d) { window.webkit.messageHandlers.probe.postMessage(JSON.stringify(d)); },
    submit: function () {}, close: function () {},
    onMessage: function (cb) { if (typeof cb === 'function') handlers.push(cb); }
  };
  window.__orcaDispatch = function (data) {
    for (var i = 0; i < handlers.length; i++) { try { handlers[i](data); } catch (e) {} }
  };
})();
"""

steps = []


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)


def on_probe(_ucm, result):
    try:
        payload = json.loads(result.get_js_value().to_string())
    except Exception as e:
        log("probe", "unparsed: %r (%s)" % (result, e))
        return
    if payload.get("action") == "log":
        log("js", payload["msg"])
    else:
        log("js", json.dumps(payload)[:200])


def run_js(view, code, cb=None):
    def done(v, res, _):
        try:
            val = v.evaluate_javascript_finish(res)
            if cb:
                cb(val.to_string() if val else "")
        except Exception as e:
            log("jserr", str(e))
            if cb:
                cb("")
    view.evaluate_javascript(code, -1, None, None, None, done, None)


def main():
    ucm = WebKit2.UserContentManager()
    ucm.register_script_message_handler("probe")
    ucm.connect("script-message-received::probe", on_probe)
    ucm.add_script(WebKit2.UserScript.new(
        BRIDGE, WebKit2.UserContentInjectedFrames.TOP_FRAME,
        WebKit2.UserScriptInjectionTime.START, None, None))

    view = WebKit2.WebView.new_with_user_content_manager(ucm)
    win = Gtk.Window(title="wkprobe")
    win.set_default_size(940, 680)
    win.add(view)
    win.show_all()

    def on_load(v, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        log("harness", "load finished")
        GLib.timeout_add(500, phase_render, v)

    view.connect("load-changed", on_load)
    view.load_html(PAGE, BASE_URI)
    Gtk.main()


def phase_render(view):
    run_js(view, "renderResults(%s); 'rendered'" % json.dumps(MODELS))
    GLib.timeout_add(2500, phase_geometry, view)
    return False


def phase_geometry(view):
    js = """(function(){
      var c = document.querySelectorAll('#results .card')[5];
      var r = c.getBoundingClientRect();
      var img = document.querySelectorAll('#results .card img')[0];
      return JSON.stringify({x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
        w:Math.round(r.width), h:Math.round(r.height),
        imgW: img ? img.naturalWidth : -1, imgComplete: img ? img.complete : null,
        scrollY: window.scrollY, docH: document.body.scrollHeight, innerH: window.innerHeight,
        onclickAttr: c.getAttribute('onclick')});
    })()"""

    def got(val):
        log("geom", val)
        try:
            g = json.loads(val)
        except Exception:
            Gtk.main_quit()
            return
        GLib.timeout_add(300, phase_click, view, g)
    run_js(view, js, got)
    return False


def phase_click(view, g):
    disp = os.environ.get("DISPLAY", ":99")
    try:
        out = subprocess.run(
            ["xdotool", "search", "--name", "wkprobe"],
            capture_output=True, text=True, env=dict(os.environ, DISPLAY=disp))
        wid = out.stdout.split()[-1]
        log("harness", "clicking card #5 at webview-relative (%d,%d) in window %s" % (g["x"], g["y"], wid))
        subprocess.run(["xdotool", "mousemove", "--window", wid, str(g["x"]), str(g["y"]),
                        "click", "1"], env=dict(os.environ, DISPLAY=disp))
    except Exception as e:
        log("harness", "xdotool failed: %s" % e)
    GLib.timeout_add(1500, phase_report, view)
    return False


def phase_report(view):
    def got(val):
        log("detail", val)
        run_js(view, "document.getElementById('det-dl-btn').click(); 'clicked'")
        GLib.timeout_add(1200, phase_nav_check, view)

    run_js(view, """(function(){
      var d = document.getElementById('detail'), r = d.getBoundingClientRect();
      return JSON.stringify({
        detailActive: d.classList.contains('active'),
        inViewport: r.top < window.innerHeight && r.bottom > 0 && r.height > 0,
        rect: [Math.round(r.top), Math.round(r.bottom), Math.round(r.height)],
        innerH: window.innerHeight,
        name: document.getElementById('det-name').textContent,
        cards: document.querySelectorAll('#results .card').length,
        status: document.getElementById('status').textContent});
    })()""", got)
    return False


def phase_nav_check(view):
    # The webview must still be on the plugin page, not on a platform login wall.
    log("nav", "uri=%s title=%s" % (view.get_uri(), view.get_title()))
    run_js(view, "document.getElementById('status').textContent",
           lambda v: (log("nav", "status=%s" % v), Gtk.main_quit()))
    return False


main()
