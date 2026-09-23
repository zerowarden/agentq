#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

function fail(message, extra = {}) {
  process.stdout.write(JSON.stringify({ ok: false, error: message, ...extra }));
  process.exit(0);
}

const argv = process.argv.slice(2);
const symbolMode = argv[0] === 'symbol';
const rootArg = symbolMode ? argv[2] : argv[1];
if (!rootArg) fail('missing repository root');
const root = fs.realpathSync(rootArg);

const DISCOVERY_CONFIG_LIMIT = 64;
const DISCOVERY_DIR_LIMIT = 2500;
const NAVIGATION_ITEM_MIN = 200;
const NAVIGATION_ITEM_FACTOR = 4;
const OPERATIONS = new Set(['definition', 'references', 'implementations']);

let ts = null;
function loadTs() {
  if (ts) return ts;
  try {
    const requireFromRoot = createRequire(path.join(root, 'package.json'));
    ts = requireFromRoot('typescript');
  } catch (error) {
    fail('TypeScript package is not resolvable from the repository; use the project\'s existing typescript dependency', { detail: String(error?.message || error), code: 'typescript_unavailable' });
  }
  return ts;
}

function runtimeMeta() {
  return { node: process.version, typescript: ts ? ts.version : null };
}

function metaPayload(discovery = null, project = null) {
  return {
    runtime: runtimeMeta(),
    discovery: {
      configs: discovery ? discovery.configs.length : 0,
      truncated: Boolean(discovery?.truncated),
      errors: discovery?.errors || [],
      limit: DISCOVERY_CONFIG_LIMIT,
    },
    project,
  };
}

function projectMeta(service, configPath) {
  let programFiles = 0;
  try {
    programFiles = service.getProgram()?.getSourceFiles()?.length || 0;
  } catch {
    programFiles = 0;
  }
  return {
    config: configPath ? rel(configPath) : null,
    root_dir: configPath ? rel(path.dirname(configPath)) : null,
    program_files: programFiles,
  };
}


function canonicalAbsolute(abs) {
  try {
    return fs.realpathSync(abs);
  } catch {
    return path.resolve(abs);
  }
}

function rel(abs) {
  const resolved = canonicalAbsolute(abs);
  const withinRoot = resolved === root || resolved.startsWith(root + path.sep);
  return withinRoot ? path.relative(root, resolved).split(path.sep).join('/') : resolved;
}

function isValidWireScope(scope) {
  if (typeof scope !== 'string' || !scope) return false;
  if (scope.includes('\\') || scope.includes('\x00')) return false;
  if (scope.startsWith('/') || /^[A-Za-z]:/.test(scope)) return false;
  if (scope === '.') return true;
  if (scope.endsWith('/')) return false;
  const parts = scope.split('/');
  return parts.every((part) => part !== '' && part !== '.' && part !== '..');
}

function parseWireScopes(scopesJson) {
  let scopes;
  try {
    scopes = JSON.parse(scopesJson || '[]');
  } catch {
    fail('invalid scope wire payload: scopes must be a JSON array', { code: 'invalid_scope' });
  }
  if (!Array.isArray(scopes)) fail('invalid scope wire payload: scopes must be a JSON array', { code: 'invalid_scope' });
  // Empty input is the repository root on the wire.
  if (!scopes.length) return ['.'];
  for (const scope of scopes) {
    if (!isValidWireScope(scope)) {
      fail(`invalid scope wire entry: ${JSON.stringify(scope)} (expected repository-relative POSIX, root is ".")`, { code: 'invalid_scope', scope });
    }
  }
  return [...new Set(scopes)];
}

function parseConfig(configPath, extraFile = undefined) {
  const configRead = ts.readConfigFile(configPath, ts.sys.readFile);
  if (configRead.error) {
    fail('failed to read tsconfig.json', { diagnostic: ts.flattenDiagnosticMessageText(configRead.error.messageText, '\n') });
  }
  const parsed = ts.parseJsonConfigFileContent(configRead.config, ts.sys, path.dirname(configPath), undefined, configPath);
  const fileNames = new Set(parsed.fileNames.map((x) => path.resolve(x)));
  if (extraFile) fileNames.add(path.resolve(extraFile));
  return { parsed, fileNames };
}

function makeService(configPath, extraFile = undefined) {
  const { parsed, fileNames } = parseConfig(configPath, extraFile);
  const versions = new Map();
  const host = {
    getScriptFileNames: () => [...fileNames],
    getScriptVersion: (name) => String(versions.get(name) || 0),
    getScriptSnapshot: (name) => {
      if (!fs.existsSync(name)) return undefined;
      return ts.ScriptSnapshot.fromString(fs.readFileSync(name, 'utf8'));
    },
    getCurrentDirectory: () => path.dirname(configPath),
    getCompilationSettings: () => parsed.options,
    getDefaultLibFileName: (options) => ts.getDefaultLibFilePath(options),
    fileExists: ts.sys.fileExists,
    readFile: ts.sys.readFile,
    readDirectory: ts.sys.readDirectory,
    directoryExists: ts.sys.directoryExists,
    getDirectories: ts.sys.getDirectories,
    realpath: ts.sys.realpath,
    useCaseSensitiveFileNames: () => ts.sys.useCaseSensitiveFileNames,
    getNewLine: () => ts.sys.newLine,
    getProjectReferences: () => parsed.projectReferences,
  };
  const service = ts.createLanguageService(host, ts.createDocumentRegistry());
  return { service, configPath };
}

function sourcePreview(source, line) {
  if (!source || line < 1) return '';
  const start = source.getPositionOfLineAndCharacter(line - 1, 0);
  return source.text.slice(start, source.getLineEndOfPosition(start)).trim().slice(0, 280);
}

function location(service, fileName, textSpan, extra = {}) {
  const abs = canonicalAbsolute(fileName);
  const source = service.getProgram()?.getSourceFile(abs);
  let start = { line: 0, character: 0 };
  let end = { line: 0, character: 0 };
  if (source) {
    start = source.getLineAndCharacterOfPosition(textSpan.start);
    end = source.getLineAndCharacterOfPosition(textSpan.start + textSpan.length);
  } else if (fs.existsSync(abs)) {
    const text = fs.readFileSync(abs, 'utf8');
    const before = text.slice(0, textSpan.start);
    start = { line: before.split('\n').length - 1, character: before.length - before.lastIndexOf('\n') - 1 };
    const spanText = text.slice(textSpan.start, textSpan.start + textSpan.length);
    const spanLines = spanText.split('\n');
    end = spanLines.length === 1
      ? { line: start.line, character: start.character + textSpan.length }
      : { line: start.line + spanLines.length - 1, character: spanLines.at(-1).length };
  }
  const withinRoot = abs === root || abs.startsWith(root + path.sep);
  return {
    path: withinRoot ? path.relative(root, abs).split(path.sep).join('/') : abs,
    external: !withinRoot,
    line: start.line + 1,
    column: start.character + 1,
    end_line: end.line + 1,
    end_column: end.character + 1,
    preview: sourcePreview(source, start.line + 1),
    ...extra,
  };
}

function uniqueLocations(items) {
  const seen = new Set();
  const unique = [];
  for (const item of items) {
    const key = `${item.path}:${item.line}:${item.column}:${item.end_line}:${item.end_column}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(item);
  }
  unique.sort((a, b) => Number(a.external) - Number(b.external) || a.path.localeCompare(b.path) || a.line - b.line || a.column - b.column);
  return unique;
}

function actionResults(service, action, file, position) {
  const raw = [];
  if (action === 'definition') {
    for (const item of service.getDefinitionAtPosition(file, position) || []) {
      raw.push(declarationLocation(service, item.fileName, item.textSpan, {
        name: item.name,
        kind: item.kind,
        container: item.containerName || '',
      }));
    }
  } else if (action === 'implementations') {
    for (const item of service.getImplementationAtPosition(file, position) || []) {
      raw.push(declarationLocation(service, item.fileName, item.textSpan, {
        name: item.name,
        kind: item.kind,
        display: item.displayParts ? ts.displayPartsToString(item.displayParts) : '',
      }));
    }
  } else if (action === 'references') {
    const groups = service.findReferences(file, position) || [];
    for (const group of groups) {
      for (const ref of group.references || []) {
        raw.push(location(service, ref.fileName, ref.textSpan, {
          definition: Boolean(ref.isDefinition),
          write: Boolean(ref.isWriteAccess),
        }));
      }
    }
  } else {
    fail(`unsupported action: ${action}`);
  }
  return uniqueLocations(raw);
}

function nodeAtPosition(source, position) {
  let found = source;
  function visit(node) {
    if (position < node.getFullStart() || position > node.getEnd()) return;
    found = node;
    node.forEachChild(visit);
  }
  visit(source);
  return found;
}

function declarationSpan(service, file, position) {
  const source = service.getProgram()?.getSourceFile(path.resolve(file));
  if (!source) return null;
  let node = nodeAtPosition(source, position);
  const isDeclaration = (value) => (
    ts.isFunctionDeclaration(value) || ts.isClassDeclaration(value) || ts.isInterfaceDeclaration(value) ||
    ts.isTypeAliasDeclaration(value) || ts.isEnumDeclaration(value) || ts.isVariableStatement(value) ||
    ts.isMethodDeclaration(value) || ts.isPropertyDeclaration(value) || ts.isConstructorDeclaration(value)
  );
  while (node && node !== source && !isDeclaration(node)) node = node.parent;
  if (!node || node === source) return null;
  const start = source.getLineAndCharacterOfPosition(node.getStart(source));
  const end = source.getLineAndCharacterOfPosition(node.getEnd());
  return { start_line: start.line + 1, end_line: end.line + 1 };
}

// A declaration location keeps the identifier span as the anchor and adds the
// enclosing declaration extent, so callers can read the whole body without
// losing the exact position semantic queries need.
function declarationLocation(service, fileName, textSpan, extra = {}) {
  return location(service, fileName, textSpan, {
    ...extra,
    declaration_span: declarationSpan(service, fileName, textSpan.start),
  });
}

function scopeAllows(fileName, scopes) {
  if (!scopes.length || scopes.includes('.')) return true;
  const relative = rel(fileName);
  // External references stay absolute here and are never treated as in-scope
  // local candidates; they must not become allowed mutation targets.
  if (path.isAbsolute(relative)) return false;
  return scopes.some((scope) => relative === scope || relative.startsWith(scope + '/'));
}

function discoverConfigs(scopes) {
  const found = new Set();
  let visited = 0;
  let truncated = false;
  const addUpward = (start) => {
    const config = ts.findConfigFile(start, ts.sys.fileExists, 'tsconfig.json');
    if (config) found.add(path.resolve(config));
  };
  addUpward(root);
  const roots = scopes.length ? scopes : ['.'];
  const skip = new Set(['node_modules', '.git', 'dist', 'build', 'coverage', '.next', '.turbo']);
  for (const scope of roots) {
    if (found.size >= DISCOVERY_CONFIG_LIMIT || visited >= DISCOVERY_DIR_LIMIT) {
      truncated = true;
      break;
    }
    const absolute = path.resolve(root, scope);
    if (!fs.existsSync(absolute)) continue;
    const start = fs.statSync(absolute).isDirectory() ? absolute : path.dirname(absolute);
    addUpward(start);
    const stack = [start];
    while (stack.length) {
      if (found.size >= DISCOVERY_CONFIG_LIMIT || visited >= DISCOVERY_DIR_LIMIT) {
        truncated = true;
        break;
      }
      const dir = stack.pop();
      visited += 1;
      const config = path.join(dir, 'tsconfig.json');
      if (fs.existsSync(config)) found.add(path.resolve(config));
      let entries = [];
      try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
      for (const entry of entries) {
        if (!entry.isDirectory() || skip.has(entry.name) || entry.name.startsWith('.')) continue;
        stack.push(path.join(dir, entry.name));
      }
    }
  }
  return { configs: [...found].sort(), truncated, visited, errors: [] };
}


function runPositionMode() {
  const [action, , fileArg, lineArg, columnArg, limitArg] = argv;
  if (!action || !fileArg || !lineArg || !columnArg) {
    fail('usage: ts_nav.mjs <definition|references|implementations> <root> <file> <line> <column> [limit]');
  }
  loadTs();
  const file = fs.realpathSync(path.isAbsolute(fileArg) ? fileArg : path.join(root, fileArg));
  const line = Number(lineArg);
  const column = Number(columnArg);
  const limit = Math.max(1, Number(limitArg || 80));
  const configPath = ts.findConfigFile(path.dirname(file), ts.sys.fileExists, 'tsconfig.json');
  if (!configPath) fail('no tsconfig.json found from target file toward filesystem root', { code: 'no_config' });
  const { service } = makeService(configPath, file);
  const sf = service.getProgram()?.getSourceFile(file);
  if (!sf) fail('target file is not part of the resolved TypeScript program', { config: configPath, code: 'target_outside_program' });
  if (!Number.isInteger(line) || line < 1 || line > sf.getLineAndCharacterOfPosition(sf.getEnd()).line + 1) fail('line is outside target file');
  const lineStart = sf.getPositionOfLineAndCharacter(line - 1, 0);
  const lineText = sf.text.slice(lineStart, sf.getLineEndOfPosition(lineStart));
  const clampedColumn = Math.max(1, Math.min(column, lineText.length + 1));
  const position = sf.getPositionOfLineAndCharacter(line - 1, clampedColumn - 1);
  const unique = actionResults(service, action, file, position);
  const discovery = { configs: [configPath], truncated: false, errors: [] };
  process.stdout.write(JSON.stringify({
    ok: true,
    action,
    root,
    config: rel(configPath),
    target: rel(file),
    line,
    column: clampedColumn,
    total: unique.length,
    shown: Math.min(unique.length, limit),
    truncated: unique.length > limit,
    results: unique.slice(0, limit),
    meta: metaPayload(discovery, projectMeta(service, configPath)),
  }));
}

function parseOperations(operationsJson) {
  let operations;
  try {
    operations = JSON.parse(operationsJson || '[]');
  } catch {
    fail('operations must be a JSON array', { code: 'invalid_operations' });
  }
  if (!Array.isArray(operations) || !operations.length) {
    fail('at least one operation is required', { code: 'invalid_operations' });
  }
  for (const operation of operations) {
    if (!OPERATIONS.has(operation)) {
      fail(`unsupported operation: ${JSON.stringify(operation)}`, { code: 'invalid_operations' });
    }
  }
  return [...new Set(operations)];
}

function operationPayload(service, action, file, position, limit) {
  try {
    const results = actionResults(service, action, file, position);
    return {
      status: 'completed',
      total: results.length,
      shown: Math.min(results.length, limit),
      truncated: results.length > limit,
      results: results.slice(0, limit),
      error: null,
    };
  } catch (error) {
    // One failed operation is a reported outcome, not a failed acquisition.
    return {
      status: 'failed',
      total: 0,
      shown: 0,
      truncated: false,
      results: [],
      error: String(error?.message || error),
    };
  }
}

function runAtMode() {
  const [, , fileArg, lineArg, columnArg, operationsArg, limitArg] = argv;
  if (!fileArg || !lineArg || !columnArg || !operationsArg) {
    fail('usage: ts_nav.mjs at <root> <file> <line> <column> <operations-json> [limit]');
  }
  const operations = parseOperations(operationsArg);
  loadTs();
  const file = fs.realpathSync(path.isAbsolute(fileArg) ? fileArg : path.join(root, fileArg));
  const line = Number(lineArg);
  const column = Number(columnArg);
  const limit = Math.max(1, Number(limitArg || 80));
  const configPath = ts.findConfigFile(path.dirname(file), ts.sys.fileExists, 'tsconfig.json');
  if (!configPath) fail('no tsconfig.json found from target file toward filesystem root', { code: 'no_config' });
  const { service } = makeService(configPath, file);
  const sf = service.getProgram()?.getSourceFile(file);
  if (!sf) fail('target file is not part of the resolved TypeScript program', { config: rel(configPath), code: 'target_outside_program' });
  if (!Number.isInteger(line) || line < 1 || line > sf.getLineAndCharacterOfPosition(sf.getEnd()).line + 1) fail('line is outside target file', { code: 'invalid_position' });
  const lineStart = sf.getPositionOfLineAndCharacter(line - 1, 0);
  const lineText = sf.text.slice(lineStart, sf.getLineEndOfPosition(lineStart));
  const clampedColumn = Math.max(1, Math.min(column, lineText.length + 1));
  const position = sf.getPositionOfLineAndCharacter(line - 1, clampedColumn - 1);
  const operationsPayload = {};
  for (const operation of operations) {
    operationsPayload[operation] = operationPayload(service, operation, file, position, limit);
  }
  const discovery = { configs: [configPath], truncated: false, errors: [] };
  process.stdout.write(JSON.stringify({
    ok: true,
    action: 'at',
    root,
    config: rel(configPath),
    target: rel(file),
    line,
    column: clampedColumn,
    declaration_span: declarationSpan(service, file, position),
    operations: operationsPayload,
    meta: metaPayload(discovery, projectMeta(service, configPath)),
  }));
}

function runProbeMode() {
  const [, , scopesJson] = argv;
  const scopes = parseWireScopes(scopesJson);
  loadTs();
  const discovery = discoverConfigs(scopes);
  process.stdout.write(JSON.stringify({
    ok: true,
    action: 'probe',
    root,
    paths: scopes,
    meta: metaPayload(discovery),
  }));
}


function runSymbolMode() {
  const [, action, , symbol, scopesJson, limitArg, pickArg] = argv;
  if (!action || !symbol) fail('usage: ts_nav.mjs symbol <action> <root> <symbol> <scopes-json> [limit] [pick]');
  // Validate the wire form before requiring the TypeScript runtime so a
  // malformed scope is always an explicit bridge-validation error, never an
  // empty complete list or a missing-runtime message.
  const scopes = parseWireScopes(scopesJson);
  loadTs();
  const limit = Math.max(1, Number(limitArg || 80));
  const pick = pickArg ? Number(pickArg) : null;
  const discovery = discoverConfigs(scopes);
  if (!discovery.configs.length) fail('no tsconfig.json found for the requested scope', { code: 'no_config' });

  const navigationLimit = Math.max(NAVIGATION_ITEM_MIN, limit * NAVIGATION_ITEM_FACTOR);
  const services = new Map();
  const candidates = [];
  const seen = new Set();
  for (const configPath of discovery.configs) {
    const project = makeService(configPath);
    services.set(configPath, project.service);
    let items = [];
    try {
      items = project.service.getNavigateToItems(symbol, navigationLimit, undefined, true, true) || [];
    } catch (error) {
      // A failed project navigation is reported, never silently skipped.
      discovery.errors.push({ config: rel(configPath), message: String(error?.message || error) });
      continue;
    }
    // Reaching the bridge-side enumeration bound means the candidate list may
    // be incomplete for this project even when the CLI limit was not hit.
    if (items.length >= navigationLimit) discovery.truncated = true;
    for (const item of items) {
      if (item.name !== symbol || !scopeAllows(item.fileName, scopes)) continue;
      const candidate = declarationLocation(
        project.service,
        item.fileName,
        item.textSpan,
        {
          name: item.name,
          kind: item.kind,
          match_kind: item.matchKind,
          container: item.containerName || '',
          config: rel(configPath),
        },
      );
      const key = `${candidate.path}:${candidate.line}:${candidate.column}`;
      if (seen.has(key)) continue;
      seen.add(key);
      candidates.push({ ...candidate, _file: canonicalAbsolute(item.fileName), _position: item.textSpan.start, _config: configPath });
    }
  }
  candidates.sort((a, b) => a.path.localeCompare(b.path) || a.line - b.line || a.column - b.column);
  const publicCandidates = candidates.map(({ _file, _position, _config, ...item }) => item);
  const base = {
    ok: true,
    action,
    resolution_mode: 'symbol',
    symbol,
    paths: scopes,
    total: candidates.length,
    shown: Math.min(candidates.length, limit),
    truncated: candidates.length > limit,
    candidates: publicCandidates.slice(0, limit),
    meta: metaPayload(discovery),
  };
  if (action === 'locate' || candidates.length === 0) {
    process.stdout.write(JSON.stringify({ ...base, ambiguous: candidates.length > 1 }));
    return;
  }
  let selectedIndex = null;
  if (pick !== null) {
    if (!Number.isInteger(pick) || pick < 1 || pick > candidates.length) fail(`pick ${pick} is outside the ${candidates.length} available candidates`);
    selectedIndex = pick - 1;
  } else if (candidates.length === 1) {
    selectedIndex = 0;
  } else {
    process.stdout.write(JSON.stringify({ ...base, ambiguous: true, hint: 'narrow --path or select one candidate with --pick N' }));
    return;
  }
  const selected = candidates[selectedIndex];
  const service = services.get(selected._config) || makeService(selected._config, selected._file).service;
  const meta = metaPayload(discovery, projectMeta(service, selected._config));
  if (action === 'overview') {
    const definitions = actionResults(service, 'definition', selected._file, selected._position);
    const references = actionResults(service, 'references', selected._file, selected._position);
    const implementations = actionResults(service, 'implementations', selected._file, selected._position);
    const truncate = (items) => ({ total: items.length, shown: Math.min(items.length, limit), truncated: items.length > limit, results: items.slice(0, limit) });
    process.stdout.write(JSON.stringify({
      ...base,
      meta,
      candidate: selectedIndex + 1,
      candidate_count: candidates.length,
      ambiguous: false,
      config: rel(selected._config),
      target: selected.path,
      line: selected.line,
      column: selected.column,
      declaration_span: declarationSpan(service, selected._file, selected._position),
      definition: truncate(definitions),
      references: truncate(references),
      implementations: truncate(implementations),
    }));
    return;
  }
  const results = actionResults(service, action, selected._file, selected._position);
  process.stdout.write(JSON.stringify({
    ...base,
    meta,
    candidate: selectedIndex + 1,
    candidate_count: candidates.length,
    ambiguous: false,
    config: rel(selected._config),
    target: selected.path,
    line: selected.line,
    column: selected.column,
    total: results.length,
    shown: Math.min(results.length, limit),
    truncated: results.length > limit,
    results: results.slice(0, limit),
  }));
}


if (symbolMode) runSymbolMode();
else if (argv[0] === 'at') runAtMode();
else if (argv[0] === 'probe') runProbeMode();
else runPositionMode();
