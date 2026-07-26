/**
 * OpenTrace exporters.
 *
 * An exporter receives finished OpenTrace records (plain objects) and is
 * responsible for persisting or forwarding them. This module provides the
 * `Exporter` interface plus the built-in `JsonlExporter`, `MemoryExporter`, and
 * `MultiExporter`, mirroring the Python SDK's `exporters/` package.
 */
import { appendFileSync, closeSync, createWriteStream, openSync, writeSync } from "node:fs";
import { createGzip, type Gzip } from "node:zlib";
import type { WriteStream } from "node:fs";

/** An OpenTrace record: the plain object emitted per JSONL line. */
export type Record = { [key: string]: unknown };

/** Exporter interface. Implement `export`; `close` is optional. */
export interface Exporter {
  export(record: Record): void;
  close(): void;
}

/** Fan a record out to several exporters (e.g. jsonl file + live OTLP). */
export class MultiExporter implements Exporter {
  private readonly exporters: Exporter[];

  constructor(exporters: Exporter[]) {
    this.exporters = [...exporters];
  }

  export(record: Record): void {
    for (const exp of this.exporters) exp.export(record);
  }

  close(): void {
    for (const exp of this.exporters) exp.close();
  }
}

/** Collect records in memory. Useful for tests and in-process analysis. */
export class MemoryExporter implements Exporter {
  readonly records: Record[] = [];

  export(record: Record): void {
    this.records.push(record);
  }

  close(): void {
    /* nothing to release */
  }
}

/** Options for {@link JsonlExporter}. */
export interface JsonlExporterOptions {
  /**
   * File path to write to. If it ends with `.gz` the output is gzip-compressed.
   * If omitted (and no `stream`), writes to stdout.
   */
  path?: string;
  /** A writable stream to use instead of a path (e.g. `process.stdout`). */
  stream?: NodeJS.WritableStream;
  /** Flush after every record (recommended for live/streaming traces). */
  flush?: boolean;
}

/**
 * JSONL exporter: writes one OpenTrace record per line to a file or stream.
 *
 * Serialization uses `JSON.stringify`, which omits `undefined`-valued keys, so
 * the records stay compact. Values are already cleaned of null/undefined by the
 * tracer before they reach here.
 */
export class JsonlExporter implements Exporter {
  private readonly flushEnabled: boolean;
  private fd: number | null = null;
  private stream: NodeJS.WritableStream | null = null;
  private gzip: Gzip | null = null;
  private ownsStream = false;
  private useStdout = false;

  constructor(pathOrOpts?: string | JsonlExporterOptions) {
    const opts: JsonlExporterOptions =
      typeof pathOrOpts === "string" ? { path: pathOrOpts } : pathOrOpts ?? {};
    this.flushEnabled = opts.flush ?? true;

    if (opts.stream) {
      this.stream = opts.stream;
    } else if (!opts.path) {
      // Default: stdout. We write via fd 1 to keep behaviour synchronous.
      this.useStdout = true;
    } else if (opts.path.endsWith(".gz")) {
      const file = createWriteStream(opts.path);
      const gz = createGzip();
      gz.pipe(file);
      this.gzip = gz;
      this.stream = gz;
      this.ownsStream = true;
    } else {
      // Append mode, opened synchronously so writes are ordered and durable.
      this.fd = openSync(opts.path, "a");
      this.ownsStream = true;
    }
  }

  export(record: Record): void {
    const line = JSON.stringify(record) + "\n";
    if (this.fd !== null) {
      writeSync(this.fd, line);
    } else if (this.useStdout) {
      writeSync(1, line);
    } else if (this.stream) {
      this.stream.write(line);
    }
  }

  close(): void {
    if (this.fd !== null) {
      closeSync(this.fd);
      this.fd = null;
    } else if (this.gzip) {
      // Ends the gzip stream, which flushes and closes the underlying file.
      this.gzip.end();
      this.gzip = null;
      this.stream = null;
    } else if (this.ownsStream && this.stream) {
      (this.stream as WriteStream).end?.();
    }
  }
}

/** Best-effort append helper used by tests that don't hold a live exporter. */
export function appendJsonl(path: string, record: Record): void {
  appendFileSync(path, JSON.stringify(record) + "\n");
}
