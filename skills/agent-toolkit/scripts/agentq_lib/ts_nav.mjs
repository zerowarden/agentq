#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

function fail(message, extra = {}) {
  process.stdout.write(JSON.stringify({ ok: false, error: message, ...extra }));
  process.exit(0);
}

const [action, rootArg, fileArg, lineArg, columnArg, limitArg] = process.argv.slice(2);
if (!action || !rootArg || !fileArg || !lineArg || !columnArg) {
  fail('usage: ts_nav.mjs <definition|references|implementations> <root> <file> <line> <column> [limit]');
}

const root = fs.realpathSync(rootArg);
const file = fs.realpathSync(path.isAbsolute(fileArg) ? fileArg : path.join(root, fileArg));
const line = Number(lineArg);
const column = Number(columnArg);
const limit = Math.max(1, Number(limitArg || 80));

let ts;
try {
  const requireFromRoot = createRequire(path.join(root, 'package.json'));
  ts = requireFromRoot('typescript');
} catch (error) {
  fail('TypeScript package is not resolvable from the repository; install/use the project\'s existing typescript dependency', { detail: String(error?.message || error) });
}

const configPath = ts.findConfigFile(path.dirname(file), ts.sys.fileExists, 'tsconfig.json');
if (!configPath) fail('no tsconfig.json found from target file toward filesystem root');

const configRead = ts.readConfigFile(configPath, ts.sys.readFile);
if (configRead.error) {
  fail('failed to read tsconfig.json', { diagnostic: ts.flattenDiagnosticMessageText(configRead.error.messageText, '\n') });
}
const parsed = ts.parseJsonConfigFileContent(configRead.config, ts.sys, path.dirname(configPath), undefined, configPath);
const fileNames = new Set(parsed.fileNames.map((x) => path.resolve(x)));
fileNames.add(file);
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
const program = service.getProgram();
const sf = program?.getSourceFile(file);
if (!sf) fail('target file is not part of the resolved TypeScript program', { config: configPath });
if (!Number.isInteger(line) || line < 1 || line > sf.getLineAndCharacterOfPosition(sf.getEnd()).line + 1) {
  fail('line is outside target file');
}
const lineStart = sf.getPositionOfLineAndCharacter(line - 1, 0);
const lineText = sf.text.slice(lineStart, sf.getLineEndOfPosition(lineStart));
const clampedColumn = Math.max(1, Math.min(column, lineText.length + 1));
const position = sf.getPositionOfLineAndCharacter(line - 1, clampedColumn - 1);

function location(fileName, textSpan, extra = {}) {
  const abs = path.resolve(fileName);
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
    ...extra,
  };
}

let raw = [];
if (action === 'definition') {
  raw = (service.getDefinitionAtPosition(file, position) || []).map((item) => location(item.fileName, item.textSpan, {
    name: item.name,
    kind: item.kind,
    container: item.containerName || '',
  }));
} else if (action === 'implementations') {
  raw = (service.getImplementationAtPosition(file, position) || []).map((item) => location(item.fileName, item.textSpan, {
    name: item.name,
    kind: item.kind,
    display: item.displayParts ? ts.displayPartsToString(item.displayParts) : '',
  }));
} else if (action === 'references') {
  const groups = service.findReferences(file, position) || [];
  for (const group of groups) {
    for (const ref of group.references || []) {
      raw.push(location(ref.fileName, ref.textSpan, {
        definition: Boolean(ref.isDefinition),
        write: Boolean(ref.isWriteAccess),
      }));
    }
  }
} else {
  fail(`unsupported action: ${action}`);
}

const seen = new Set();
const unique = [];
for (const item of raw) {
  const key = `${item.path}:${item.line}:${item.column}:${item.end_line}:${item.end_column}`;
  if (seen.has(key)) continue;
  seen.add(key);
  unique.push(item);
}
unique.sort((a, b) => a.external - b.external || a.path.localeCompare(b.path) || a.line - b.line || a.column - b.column);

process.stdout.write(JSON.stringify({
  ok: true,
  action,
  root,
  config: path.relative(root, configPath).split(path.sep).join('/'),
  target: path.relative(root, file).split(path.sep).join('/'),
  line,
  column: clampedColumn,
  total: unique.length,
  shown: Math.min(unique.length, limit),
  truncated: unique.length > limit,
  results: unique.slice(0, limit),
}));
