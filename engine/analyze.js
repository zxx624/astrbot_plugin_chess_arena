#!/usr/bin/env node
// Local xqwlight analyzer wrapper for AstrBot chess arena plugin.
// Reads JSON from stdin: {fen, legal_moves, depth, timeout_ms}
// Writes JSON to stdout: {best_move, score, depth, nodes, engine:'local_xqwlight'} or {error, engine:'local_xqwlight'}

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ENGINE = 'local_xqwlight';

// Keep the command-line wrapper deterministic across runs; xqwlight uses
// Math.random for tie-breaking/Zobrist initialization, and deterministic output
// is easier to validate in plugin smoke tests.
Math.random = function deterministicRandom() { return 0.5; };

function output(obj) {
  process.stdout.write(JSON.stringify(Object.assign({ engine: ENGINE }, obj)) + '\n');
}

function fail(message, extra) {
  output(Object.assign({ error: String(message || 'unknown error') }, extra || {}));
}

function loadScript(filePath) {
  let code = fs.readFileSync(filePath, 'utf8');
  // xqwlight files declare globals with var/function; strict mode in vm can make
  // old browser-oriented code awkward, so mirror the server wrapper behavior.
  code = code.replace(/['\"]use strict['\"];?\s*/g, '');
  vm.runInThisContext(code, { filename: filePath });
}

function loadEngine() {
  const base = __dirname;
  loadScript(path.join(base, 'cchess.js'));
  loadScript(path.join(base, 'position.js'));
  loadScript(path.join(base, 'search.js'));
  loadScript(path.join(base, 'book.js'));
}

// === UCCI <-> xqwlight coordinate conversion ===
// xqwlight: 16x16 board, FILE_LEFT=3, RANK_TOP=3
// UCCI: file a-i = col 0-8, rank 0-9 (rank 0 = red home = bottom)
function xqwlightSqToUcci(sq) {
  const y = sq >> 4;
  const x = sq & 15;
  return { file: x - 3, rank: 12 - y };
}

function moveToUcci(mv) {
  const src = xqwlightSqToUcci(SRC(mv));
  const dst = xqwlightSqToUcci(DST(mv));
  return String.fromCharCode('a'.charCodeAt(0) + src.file) + String(src.rank) +
    String.fromCharCode('a'.charCodeAt(0) + dst.file) + String(dst.rank);
}

function fenToXqwlight(fen) {
  const parts = String(fen || '').trim().split(/\s+/);
  if (parts.length >= 2) {
    // Arena/UCCI uses r/b; xqwlight fromFen treats b as black-to-move and any
    // other value as red/white-to-move.
    parts[1] = parts[1] === 'r' ? 'w' : 'b';
  }
  return parts.join(' ');
}

function normalizeLegalMoves(legalMoves) {
  if (!Array.isArray(legalMoves)) return [];
  return legalMoves.map((m) => String(m || '').trim()).filter(Boolean);
}

function clampInt(value, fallback, min, max) {
  const n = Number.parseInt(value, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function analyze(input) {
  const fen = String(input && input.fen || '').trim();
  if (!fen) {
    return { error: 'missing fen' };
  }

  const legalMoves = normalizeLegalMoves(input.legal_moves);
  const depth = clampInt(input.depth, 3, 1, 6);
  const timeoutMs = clampInt(input.timeout_ms, 10000, 100, 60000);

  const pos = new Position();
  pos.fromFen(fenToXqwlight(fen));

  const search = new Search(pos, 16);
  const mv = search.searchMain(depth, timeoutMs);
  const bestMove = mv > 0 ? moveToUcci(mv) : '';

  if (!bestMove) {
    return { error: 'no move found', depth, nodes: search.allNodes || 0 };
  }
  if (legalMoves.length > 0 && !legalMoves.includes(bestMove)) {
    return {
      error: 'best_move not in legal_moves',
      best_move: bestMove,
      depth,
      nodes: search.allNodes || 0,
    };
  }

  return {
    best_move: bestMove,
    score: 0,
    depth,
    nodes: search.allNodes || 0,
  };
}

function main() {
  let body = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { body += chunk; });
  process.stdin.on('end', () => {
    try {
      const input = body.trim() ? JSON.parse(body) : {};
      loadEngine();
      output(analyze(input));
    } catch (err) {
      fail(err && err.message ? err.message : String(err));
    }
  });
}

main();
