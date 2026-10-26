/** Image-owned bounded Vitest 5 reporter. Reports are source self-reports, not execution proof. */
import * as fs from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";

export const DETAIL_BYTES = 512 * 1024;
export const SUMMARY_BYTES = 64 * 1024;
export const MAX_CASES = 10_000;
export const MAX_MODULES = 1_000;
const SCHEMA = "engineering-hub.modular-ci-vitest.v1";
const HEAD = '{"schema":"engineering-hub.modular-ci-vitest-details.v1","records":[';
const TAIL = ']}\n';

function requireValue(value) {
  if (!value) throw new Error("bounded_vitest_report_unavailable");
}

function checkDirectory(directory, io) {
  requireValue(typeof directory === "string" && path.isAbsolute(directory));
  let current = path.resolve(directory);
  for (;;) {
    const item = io.lstatSync(current);
    requireValue(item.isDirectory() && !item.isSymbolicLink());
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
}

/** Exclusive owned temporary file, fsync, then rename: no empty success on ENOSPC.
 * A failed bounded temp is intentionally retained as negative evidence, never retried.
 */
export function atomicJSON(directory, name, text, maximum, io = fs) {
  requireValue(["ui-detail.json", "ui-summary.json"].includes(name));
  requireValue(typeof text === "string" && Buffer.byteLength(text, "utf8") <= maximum);
  checkDirectory(directory, io);
  const destination = path.join(directory, name);
  try {
    io.lstatSync(destination);
    throw new Error("bounded_vitest_report_exists");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const temporary = path.join(directory, "." + name + "." + randomBytes(12).toString("hex") + ".tmp");
  let descriptor;
  try {
    descriptor = io.openSync(temporary, fs.constants.O_CREAT | fs.constants.O_EXCL |
      fs.constants.O_WRONLY | (fs.constants.O_NOFOLLOW ?? 0), 0o600);
    const bytes = Buffer.from(text, "utf8");
    let offset = 0;
    while (offset < bytes.length) {
      const written = io.writeSync(descriptor, bytes, offset, bytes.length - offset);
      requireValue(Number.isInteger(written) && written > 0);
      offset += written;
    }
    io.fsyncSync(descriptor);
    io.closeSync(descriptor); descriptor = undefined;
    io.renameSync(temporary, destination);
    // Directory fsync is supported in the Linux image. Windows diagnostics only
    // verify atomic publication, not Linux crash durability.
    if (process.platform !== "win32") {
      const parent = io.openSync(directory, fs.constants.O_RDONLY);
      try { io.fsyncSync(parent); } finally { io.closeSync(parent); }
    }
  } finally {
    if (descriptor !== undefined) io.closeSync(descriptor);
  }
}

/** Explicit operator/test construction; production default only accepts /test-output. */
export class BoundedVitestReporter {
  constructor({ directory, io = fs } = {}) {
    checkDirectory(directory, io);
    this.directory = directory;
    this.io = io;
    this.records = [];
    this.detailBytes = Buffer.byteLength(HEAD + TAIL);
    this.omittedRecords = this.truncatedFields = this.captureEvents = this.omittedCaptures = 0;
    this.planned = null;
    this.ended = false;
    this.protocolFailed = false;
  }

  increment(field, amount = 1) {
    const value = this[field] + amount;
    if (!Number.isSafeInteger(value) || value < 0) {
      this.protocolFailed = true;
      this[field] = Number.MAX_SAFE_INTEGER;
    } else this[field] = value;
  }

  clip(value, limit) {
    if (typeof value !== "string") {
      this.protocolFailed = true;
      return "";
    }
    // Never encode/copy an arbitrarily large original string. UTF-8 expansion
    // of this limited prefix is bounded too; final JSON escaping counts again.
    const prefix = value.slice(0, limit);
    const encoded = Buffer.from(prefix, "utf8");
    let result = encoded.subarray(0, limit).toString("utf8");
    while (Buffer.byteLength(result, "utf8") > limit) result = result.slice(0, -1);
    if (prefix.length !== value.length || encoded.length > limit) this.increment("truncatedFields");
    return result;
  }

  record(value, capture = false) {
    const encoded = JSON.stringify(value);
    const size = Buffer.byteLength(encoded) + (this.records.length ? 1 : 0);
    if (this.detailBytes + size > DETAIL_BYTES) {
      this.increment("omittedRecords");
      if (capture) this.increment("omittedCaptures");
      return;
    }
    this.records.push(encoded);
    this.detailBytes += size;
  }

  onTestRunStart(specifications) {
    requireValue(this.planned === null && Array.isArray(specifications));
    this.planned = specifications.length;
    if (this.planned > MAX_MODULES) this.protocolFailed = true;
  }

  onUserConsoleLog(log) {
    if (!Number.isSafeInteger(this.captureEvents + 1)) { this.protocolFailed = true; return; }
    this.increment("captureEvents");
    this.record({ kind: "console", type: this.clip(log?.type ?? "unknown", 32),
      content: this.clip(log?.content ?? "", 2048) }, true);
  }

  error(value, label) {
    const error = value && typeof value === "object" ? value : {};
    this.record({ kind: "error", test: this.clip(label, 1024),
      message: this.clip(typeof error.message === "string" ? error.message : "Unknown error", 16 * 1024),
      stack: this.clip(typeof error.stack === "string" ? error.stack : "", 16 * 1024) });
  }

  onTestRunEnd(modules, unhandledErrors, reason) {
    requireValue(!this.ended);
    this.ended = true;
    const outcomes = { passed: 0, failed: 0, skipped: 0, pending: 0, unknown: 0, total: 0 };
    let observed = 0, failedModules = 0, observedModules = 0, caseLimitReached = false;
    let enumerationComplete = this.planned !== null && Array.isArray(modules) && Array.isArray(unhandledErrors);
    const finishReason = ["passed", "failed", "interrupted"].includes(reason) ? reason : "unknown";
    if (finishReason === "unknown" || finishReason === "interrupted") enumerationComplete = false;
    if (Array.isArray(modules)) {
      observedModules = modules.length;
      if (modules.length !== this.planned || modules.length > MAX_MODULES) enumerationComplete = false;
      outer: for (const module of modules.slice(0, MAX_MODULES)) {
        try {
          const state = module.state();
          if (state === "failed") failedModules++;
          else if (!["passed", "skipped"].includes(state)) enumerationComplete = false;
          const errors = module.errors();
          requireValue(Array.isArray(errors));
          for (const error of errors.slice(0, 8)) this.error(error, "Module loading or setup");
          this.increment("omittedRecords", Math.max(0, errors.length - 8));
          if (errors.length && state !== "failed") enumerationComplete = false;
          for (const test of module.children.allTests()) {
            if (observed >= MAX_CASES) { enumerationComplete = false; caseLimitReached = true; break outer; }
            observed++;
            const result = test.result();
            const state = result?.state;
            if (["passed", "failed", "skipped", "pending"].includes(state)) outcomes[state]++;
            else { outcomes.unknown++; enumerationComplete = false; }
            if (state === "failed") {
              const errors = result.errors;
              requireValue(Array.isArray(errors));
              for (const error of errors.slice(0, 8)) this.error(error, test.fullName);
              this.increment("omittedRecords", Math.max(0, errors.length - 8));
            }
          }
        } catch {
          enumerationComplete = false;
        }
      }
    }
    const unhandled = Array.isArray(unhandledErrors) ? unhandledErrors.length : 0;
    for (const error of (Array.isArray(unhandledErrors) ? unhandledErrors.slice(0, 8) : []))
      this.error(error, "Unhandled error");
    this.increment("omittedRecords", Math.max(0, unhandled - 8));
    outcomes.total = outcomes.passed + outcomes.failed + outcomes.skipped;
    enumerationComplete &&= outcomes.pending === 0 && outcomes.unknown === 0;
    const details = HEAD + this.records.join(",") + TAIL;
    requireValue(Buffer.byteLength(details) === this.detailBytes && this.detailBytes <= DETAIL_BYTES);
    let detailWritten = false;
    try { atomicJSON(this.directory, "ui-detail.json", details, DETAIL_BYTES, this.io); detailWritten = true; }
    catch { this.protocolFailed = true; }
    const complete = enumerationComplete && !this.protocolFailed && detailWritten &&
      this.omittedRecords === 0 && this.truncatedFields === 0;
    const report = { schema: SCHEMA, complete,
      state: complete && finishReason === "passed" && outcomes.failed === 0 && unhandled === 0 &&
        failedModules === 0 && outcomes.total > 0 ? "passed" : "failed",
      reason: finishReason, outcomes, observed_cases: observed,
      counts_complete: enumerationComplete, case_limit_reached: caseLimitReached,
      module_limit_reached: observedModules > MAX_MODULES,
      modules: { planned: this.planned, observed: observedModules, failed: failedModules },
      unhandled_errors: unhandled,
      details: { path: "ui-detail.json", bytes: this.detailBytes, records: this.records.length,
        omitted_records: this.omittedRecords, truncated_fields: this.truncatedFields,
        capture_events: this.captureEvents, omitted_captures: this.omittedCaptures } };
    if (report.state !== "passed" && !process.exitCode) process.exitCode = 1;
    try { atomicJSON(this.directory, "ui-summary.json", JSON.stringify(report) + "\n", SUMMARY_BYTES, this.io); }
    catch {
      if (!process.exitCode) process.exitCode = 1;
      throw new Error("bounded_vitest_report_unavailable");
    }
    return report;
  }
}

export default class ModularCIVitestReporter extends BoundedVitestReporter {
  constructor() {
    requireValue(process.env.EH_CI_VITEST_REPORT_DIR === "/test-output");
    super({ directory: process.env.EH_CI_VITEST_REPORT_DIR });
  }
}
