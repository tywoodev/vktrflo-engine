/**
 * console-relay.js
 * Intercepts browser console output and forwards it to the VectorFlow
 * telemetry endpoint, which relays entries into the local Redis instance.
 *
 * Behaviour:
 *  - Hooks console.log / info / warn / error / debug
 *  - Buffers entries and flushes every FLUSH_INTERVAL_MS or when the
 *    buffer reaches FLUSH_BATCH_SIZE, whichever comes first
 *  - Drops silently on flush failure (non-critical telemetry path)
 *  - Guards against infinite recursion from its own fetch calls
 */

const ENDPOINT   = "/api/telemetry/console";
const KEY        = "vktrflo:console";
const FLUSH_INTERVAL_MS = 1000;
const FLUSH_BATCH_SIZE  = 20;
const MAX_MSG_LENGTH    = 4096;

const _LEVELS = ["log", "info", "warn", "error", "debug"];
const _orig   = {};
let   _buffer = [];
let   _flushing = false;

function _serialize(args) {
  try {
    return args
      .map(a => {
        if (typeof a === "string") return a;
        try { return JSON.stringify(a); } catch { return String(a); }
      })
      .join(" ")
      .slice(0, MAX_MSG_LENGTH);
  } catch {
    return "[unserializable]";
  }
}

function _entry(level, args) {
  return { ts: Date.now(), level, msg: _serialize(Array.from(args)) };
}

async function _flush() {
  if (_flushing || _buffer.length === 0) return;
  _flushing = true;
  const batch = _buffer.splice(0, _buffer.length);
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: KEY, entries: batch }),
      keepalive: true,
    });
  } catch {
    // Non-critical — drop silently
  } finally {
    _flushing = false;
  }
}

function _hook() {
  for (const level of _LEVELS) {
    _orig[level] = console[level].bind(console);
    console[level] = function (...args) {
      _orig[level](...args);
      _buffer.push(_entry(level, args));
      if (_buffer.length >= FLUSH_BATCH_SIZE) _flush();
    };
  }
}

_hook();
setInterval(_flush, FLUSH_INTERVAL_MS);
window.addEventListener("beforeunload", _flush);

_orig.log("[VF:telemetry] console relay active →", ENDPOINT);
